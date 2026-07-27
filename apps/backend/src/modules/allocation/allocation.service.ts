import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { AllocationSnapshot } from '../../entities/allocation-snapshot.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { Strategy, StrategyStatus } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { config } from '../../config/configuration';

const MAX_TICKER_ALLOCATION_PCT = config.risk.allocationDiversificationCapPct;
const MIN_PERFORMANCE_MULTIPLIER = 0.5;
const MAX_PERFORMANCE_MULTIPLIER = 1.5;

@Injectable()
export class AllocationService {
  constructor(
    @InjectRepository(AllocationSnapshot)
    private readonly allocationRepo: Repository<AllocationSnapshot>,
    @InjectRepository(ProviderBalanceSnapshot)
    private readonly balanceRepo: Repository<ProviderBalanceSnapshot>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
    @InjectRepository(BacktestResult)
    private readonly backtestRepo: Repository<BacktestResult>,
  ) {}

  /**
   * Equal-weight base, adjusted by a performance multiplier from each ticker's live
   * strategy Sharpe (spec Section 6) — probationary/weak strategies get less, strong
   * ones get modestly more — then capped by the diversification limit and
   * renormalized so the pool still sums to 100%.
   */
  async rebalanceProviderPool(providerId: string) {
    const latestBalance = await this.balanceRepo.findOne({
      where: { providerId },
      order: { syncedAt: 'DESC' },
    });
    const poolCapital = latestBalance ? Number(latestBalance.tradableBalance) : 0;

    const activeTickers = await this.tickerRepo.find({
      where: { providerId, status: TickerStatus.ACTIVE },
    });

    if (activeTickers.length === 0) return [];

    const weights = new Map<string, number>();
    for (const ticker of activeTickers) {
      const multiplier = await this.performanceMultiplier(ticker.id);
      weights.set(ticker.id, multiplier);
    }

    const totalWeight = [...weights.values()].reduce((a, b) => a + b, 0);
    const rawPct = new Map<string, number>();
    for (const ticker of activeTickers) {
      rawPct.set(ticker.id, (weights.get(ticker.id)! / totalWeight) * 100);
    }

    // Diversification cap: clip anything over the max, then redistribute the excess
    // proportionally across the tickers still under the cap.
    const cappedPct = this.applyDiversificationCap(rawPct);

    const snapshots: AllocationSnapshot[] = [];
    for (const ticker of activeTickers) {
      const pct = cappedPct.get(ticker.id)!;
      const allocatedCapital = (poolCapital * pct) / 100;
      const snapshot = this.allocationRepo.create({
        providerId,
        tickerId: ticker.id,
        allocatedCapital,
        allocationPct: Number(pct.toFixed(2)),
      });
      snapshots.push(await this.allocationRepo.save(snapshot));
    }

    return snapshots;
  }

  private async performanceMultiplier(tickerId: string): Promise<number> {
    const liveStrategy = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
    if (!liveStrategy) return 1.0;

    const backtest = await this.backtestRepo.findOne({ where: { strategyId: liveStrategy.id } });
    if (!backtest) return 1.0;

    // Map Sharpe onto [MIN, MAX] via a simple linear scale centered at Sharpe=1.0 (the
    // default minSharpe promotion threshold) — a strategy right at the bar gets a
    // neutral 1.0x weight, meaningfully better/worse ones scale within the clamp.
    const sharpe = Number(backtest.sharpe);
    const raw = 1.0 + (sharpe - 1.0) * 0.25;
    return Math.min(MAX_PERFORMANCE_MULTIPLIER, Math.max(MIN_PERFORMANCE_MULTIPLIER, raw));
  }

  private applyDiversificationCap(rawPct: Map<string, number>): Map<string, number> {
    const result = new Map(rawPct);
    // Iteratively clip-and-redistribute until nothing exceeds the cap (or only one
    // ticker remains, which trivially takes 100%).
    for (let iter = 0; iter < result.size; iter++) {
      const overCap = [...result.entries()].filter(([, pct]) => pct > MAX_TICKER_ALLOCATION_PCT);
      if (overCap.length === 0) break;

      let excess = 0;
      for (const [id] of overCap) {
        excess += result.get(id)! - MAX_TICKER_ALLOCATION_PCT;
        result.set(id, MAX_TICKER_ALLOCATION_PCT);
      }

      const underCapIds = [...result.entries()].filter(([, pct]) => pct < MAX_TICKER_ALLOCATION_PCT).map(([id]) => id);
      if (underCapIds.length === 0) break;
      const underCapTotal = underCapIds.reduce((sum, id) => sum + result.get(id)!, 0);
      for (const id of underCapIds) {
        const share = underCapTotal > 0 ? result.get(id)! / underCapTotal : 1 / underCapIds.length;
        result.set(id, result.get(id)! + excess * share);
      }
    }
    return result;
  }
}
