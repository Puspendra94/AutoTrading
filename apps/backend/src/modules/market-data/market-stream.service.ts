import { Injectable, Logger, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Not, Repository } from 'typeorm';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { Position, PositionStatus, PositionSide } from '../../entities/position.entity';
import { BinanceAdapter, LiveKlineTick } from '../provider/adapters/binance.adapter';
import { MarketDataService } from './market-data.service';
import { TradingGateway } from '../../websockets/trading.gateway';

/**
 * Owns live Binance kline WebSocket subscriptions for every ACTIVE ticker. Runs inside
 * the API process (not the worker) since it feeds TradingGateway directly — see
 * CONVENTIONS.md and PROGRESS.md for why live streaming isn't one of the worker's
 * scheduled jobs (it's a persistent connection, not an interval job).
 */
@Injectable()
export class MarketStreamService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(MarketStreamService.name);
  private readonly activeStreams = new Map<string, any>(); // tickerId -> WebsocketStream handle

  constructor(
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(Position) private readonly positionRepo: Repository<Position>,
    private readonly binanceAdapter: BinanceAdapter,
    private readonly marketDataService: MarketDataService,
    private readonly tradingGateway: TradingGateway,
  ) {}

  async onModuleInit() {
    // Stream for any ticker that has real data behind it (ACTIVE = live-strategy-managed,
    // ONBOARDING = added but strategy generation hasn't completed yet) — the dashboard's
    // live chart (spec 16.1) is useful for any managed ticker, not only ones with a live
    // strategy. INACTIVE means backfill failed; nothing to stream.
    const streamableTickers = await this.tickerRepo.find({ where: { status: Not(TickerStatus.INACTIVE) } });
    for (const ticker of streamableTickers) {
      this.startStream(ticker);
    }
  }

  onModuleDestroy() {
    for (const stream of this.activeStreams.values()) {
      stream.disconnect?.();
    }
  }

  startStream(ticker: Ticker) {
    if (this.activeStreams.has(ticker.id)) return;

    const stream = this.binanceAdapter.streamKlines(
      ticker.symbol,
      ticker.interval,
      (tick) => this.handleTick(ticker.id, tick),
      (err) => this.logger.error(`Stream error for ${ticker.symbol} (${ticker.id}): ${err.message}`),
    );
    this.activeStreams.set(ticker.id, stream);
    this.logger.log(`Started live stream for ${ticker.symbol} (${ticker.id})`);
  }

  stopStream(tickerId: string) {
    const stream = this.activeStreams.get(tickerId);
    if (stream) {
      stream.disconnect?.();
      this.activeStreams.delete(tickerId);
    }
  }

  private async handleTick(tickerId: string, tick: LiveKlineTick) {
    const candle = {
      timestamp: new Date(tick.openTime),
      open: tick.open,
      high: tick.high,
      low: tick.low,
      close: tick.close,
      volume: tick.volume,
    };

    // Push to subscribed frontend clients immediately, regardless of candle finality —
    // the chart wants every intra-candle tick, not just closes.
    this.tradingGateway.broadcastPriceUpdate(tickerId, { ...candle, isFinal: tick.isFinal });

    // Only persist to TimescaleDB on candle close to avoid rewriting the same row on
    // every ~2s tick; upsert handles the case where it arrives more than once.
    if (tick.isFinal) {
      await this.marketDataService.upsertLiveCandle(tickerId, candle);
    }

    await this.updateOpenPositionsMarkPrice(tickerId, tick.close);
  }

  private async updateOpenPositionsMarkPrice(tickerId: string, lastPrice: number) {
    const openPositions = await this.positionRepo.find({ where: { tickerId, status: PositionStatus.OPEN } });
    if (openPositions.length === 0) return;

    for (const position of openPositions) {
      const entry = Number(position.entryPrice);
      const qty = Number(position.quantity);
      const unrealized =
        position.side === PositionSide.LONG ? (lastPrice - entry) * qty : (entry - lastPrice) * qty;
      position.currentPrice = lastPrice;
      position.unrealizedPl = Number(unrealized.toFixed(8));
      await this.positionRepo.save(position);
    }

    await this.tradingGateway.broadcastPositionUpdate();
  }
}
