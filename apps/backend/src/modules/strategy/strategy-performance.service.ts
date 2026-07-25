import { Injectable, Logger, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { MoreThan, Repository } from 'typeorm';
import { Strategy } from '../../entities/strategy.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { StrategyPerformance, PerformanceStatus } from '../../entities/strategy-performance.entity';

// Same execution frictions the backtest evaluator applies, so the "real data" replay is
// consistent with the numbers a strategy was gated on.
const ENTRY_SLIP = 1.0005; // +0.05% on entry
const EXIT_SLIP = 0.9995; // -0.05% on exit
const ROUNDTRIP_FEE = 0.0015; // 0.15% fees per round trip
const NOTIONAL = 10000; // starting equity in USD
const READ_BATCH = 50000; // candles pulled per DB page during the streaming replay
const MAX_STORED_TRADES = 300; // cap on the per-trade detail we persist (counts stay exact)

@Injectable()
export class StrategyPerformanceService {
  private readonly logger = new Logger(StrategyPerformanceService.name);
  private readonly inFlight = new Set<string>();

  constructor(
    @InjectRepository(StrategyPerformance)
    private readonly perfRepo: Repository<StrategyPerformance>,
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
    @InjectRepository(OhlcvData)
    private readonly ohlcvRepo: Repository<OhlcvData>,
  ) {}

  async getPerformance(strategyId: string) {
    return this.perfRepo.findOne({ where: { strategyId } });
  }

  /** Fire-and-forget: kick off (or re-run) the replay without blocking the caller. */
  computeForStrategyInBackground(strategyId: string) {
    if (this.inFlight.has(strategyId)) return;
    this.inFlight.add(strategyId);
    // Defer so the HTTP request that triggered it returns immediately.
    setImmediate(() => {
      this.computeForStrategy(strategyId)
        .catch((e) => this.logger.error(`Performance replay failed for ${strategyId}: ${e.message}`))
        .finally(() => this.inFlight.delete(strategyId));
    });
  }

  /**
   * Replays the strategy's entry/exit rules over EVERY stored candle for its ticker
   * (streamed in pages so we never hold millions of rows at once), pairs buy->sell into
   * round-trip trades, and records intents / trades / P&L / drawdown. Idempotent per
   * strategy (single row, upserted).
   */
  async computeForStrategy(strategyId: string): Promise<StrategyPerformance> {
    const strategy = await this.strategyRepo.findOne({ where: { id: strategyId } });
    if (!strategy) throw new NotFoundException('Strategy not found');

    let row = await this.perfRepo.findOne({ where: { strategyId } });
    if (!row) row = this.perfRepo.create({ strategyId });
    row.status = PerformanceStatus.PENDING;
    row.error = null as any;
    await this.perfRepo.save(row);

    try {
      const result = await this.replay(strategy.tickerId, strategy.parametersJson);
      Object.assign(row, result, { status: PerformanceStatus.READY, error: null });
      return await this.perfRepo.save(row);
    } catch (err: any) {
      row.status = PerformanceStatus.FAILED;
      row.error = err?.message ?? String(err);
      await this.perfRepo.save(row);
      throw err;
    }
  }

  private async replay(tickerId: string, params: any): Promise<Partial<StrategyPerformance>> {
    const emaFast = params?.indicatorConfig?.emaFastPeriod || 12;
    const emaSlow = params?.indicatorConfig?.emaSlowPeriod || 26;
    const stopLossPct = (params?.indicatorConfig?.stopLossPct || 1.5) / 100;
    const takeProfitPct = (params?.indicatorConfig?.takeProfitPct || 3.5) / 100;
    const kFast = 2 / (emaFast + 1);
    const kSlow = 2 / (emaSlow + 1);

    let seeded = false;
    let fast = 0, slow = 0, prevFast = 0, prevSlow = 0;
    let barIndex = 0;

    let position: 'NONE' | 'LONG' = 'NONE';
    let entryPrice = 0;
    let entryTime: Date | null = null;

    let equity = NOTIONAL, peakEquity = NOTIONAL, maxDD = 0;
    let intentCount = 0, tradeCount = 0, wins = 0;
    let sumRet = 0, sumRetSq = 0;
    const trades: Record<string, any>[] = [];

    let candleCount = 0;
    let dataFrom: Date | null = null;
    let dataTo: Date | null = null;

    let cursor = new Date(0);
    // Keyset pagination over the raw 1m base series (ASC), carrying EMA/position state
    // across pages so the replay is a single continuous pass.
    for (;;) {
      const rows = await this.ohlcvRepo.find({
        where: { tickerId, timestamp: MoreThan(cursor) },
        order: { timestamp: 'ASC' },
        take: READ_BATCH,
        select: ['timestamp', 'close'],
      });
      if (!rows.length) break;

      for (const c of rows) {
        const price = Number(c.close);
        candleCount++;
        if (!dataFrom) dataFrom = c.timestamp;
        dataTo = c.timestamp;
        if (!Number.isFinite(price)) continue;

        if (!seeded) {
          fast = slow = prevFast = prevSlow = price;
          seeded = true;
          barIndex = 1;
          continue;
        }
        prevFast = fast;
        prevSlow = slow;
        fast = price * kFast + fast * (1 - kFast);
        slow = price * kSlow + slow * (1 - kSlow);
        barIndex++;
        if (barIndex < emaSlow) continue; // EMA warm-up

        if (position === 'NONE') {
          if (fast > slow && prevFast <= prevSlow) {
            position = 'LONG';
            entryPrice = price * ENTRY_SLIP;
            entryTime = c.timestamp;
            intentCount++; // buy intent
          }
        } else {
          const gross = (price - entryPrice) / entryPrice;
          let reason = '';
          if (gross <= -stopLossPct) reason = 'Stop loss';
          else if (gross >= takeProfitPct) reason = 'Take profit';
          else if (fast < slow) reason = 'EMA cross down';
          if (reason) {
            const exitPrice = price * EXIT_SLIP;
            const net = (exitPrice - entryPrice) / entryPrice - ROUNDTRIP_FEE;
            const pnl = equity * net;
            equity += pnl;
            if (equity > peakEquity) peakEquity = equity;
            const dd = ((peakEquity - equity) / peakEquity) * 100;
            if (dd > maxDD) maxDD = dd;

            tradeCount++;
            intentCount++; // sell intent
            if (net > 0) wins++;
            sumRet += net;
            sumRetSq += net * net;
            trades.push({
              entryTime: entryTime,
              entryPrice: Number(entryPrice.toFixed(2)),
              exitTime: c.timestamp,
              exitPrice: Number(exitPrice.toFixed(2)),
              side: 'long',
              returnPct: Number((net * 100).toFixed(3)),
              pnlUsd: Number(pnl.toFixed(2)),
              reason,
            });
            if (trades.length > MAX_STORED_TRADES * 2) trades.splice(0, trades.length - MAX_STORED_TRADES);
            position = 'NONE';
          }
        }
      }

      cursor = rows[rows.length - 1].timestamp;
      if (rows.length < READ_BATCH) break;
    }

    const mean = tradeCount > 0 ? sumRet / tradeCount : 0;
    const variance = tradeCount > 1 ? Math.max(0, sumRetSq / tradeCount - mean * mean) : 0;
    const std = Math.sqrt(variance);
    const sharpe = std > 0 ? (mean / std) * Math.sqrt(tradeCount) : 0;

    return {
      dataFrom: dataFrom ?? null,
      dataTo: dataTo ?? null,
      candleCount,
      intentCount,
      tradeCount,
      openPositions: position === 'LONG' ? 1 : 0,
      winRate: Number((tradeCount > 0 ? (wins / tradeCount) * 100 : 0).toFixed(1)),
      totalReturnPct: Number(((equity / NOTIONAL - 1) * 100).toFixed(2)),
      totalPnlUsd: Number((equity - NOTIONAL).toFixed(2)),
      maxDrawdownPct: Number(maxDD.toFixed(2)),
      sharpe: Number(sharpe.toFixed(2)),
      avgTradeReturnPct: Number((mean * 100).toFixed(3)),
      notionalUsd: NOTIONAL,
      // Most-recent trades first, bounded.
      tradesJson: trades.slice(-MAX_STORED_TRADES).reverse(),
    };
  }
}
