import { Inject, Injectable, Logger, OnModuleInit, OnModuleDestroy } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Not, Repository } from 'typeorm';
import Redis from 'ioredis';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { BinanceAdapter, LiveKlineTick } from '../provider/adapters/binance.adapter';
import { MarketDataService } from './market-data.service';
import { MarketDataSeedService } from './market-data-seed.service';
import { TradingGateway } from '../../websockets/trading.gateway';
import { REDIS_SUBSCRIBER } from '../../common/redis/redis.module';
import { config } from '../../config/configuration';

// Must match worker-py's redis_bus channel names.
export const MARKET_TICK_CHANNEL = 'market:tick';
export const POSITIONS_UPDATE_CHANNEL = 'positions:update';
export const SIGNALS_UPDATE_CHANNEL = 'signals:update';

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

  // 'internal' (default) = own the Binance WS in-process (legacy). 'redis' = consume live
  // ticks the Python worker publishes to market:tick — the worker then owns ingestion and
  // is the writer of record for candles (Phase 1, see MIGRATION.md). Never run the worker's
  // live stream AND 'internal' at once (double ingestion).
  private readonly liveStreamSource: 'internal' | 'redis' = config.liveStreamSource;

  // Who runs the live trading loop (mark-price, hard exits, evaluate+execute). 'backend'
  // (default) keeps it here; 'worker' means the Python worker owns execution (Phase 3c-3),
  // so this process must NOT also execute — it only broadcasts price + forwards position
  // updates the worker publishes. Set to 'worker' only together with the worker's
  // WORKER_OWNS_EXECUTION, never one alone (double orders / no execution).
  private readonly liveExecutionSource: 'backend' | 'worker' = config.liveExecutionSource;

  constructor(
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    private readonly binanceAdapter: BinanceAdapter,
    private readonly marketDataService: MarketDataService,
    private readonly marketDataSeedService: MarketDataSeedService,
    private readonly tradingGateway: TradingGateway,
    @Inject(REDIS_SUBSCRIBER) private readonly redisSubscriber: Redis,
  ) {}

  async onModuleInit() {
    // Ensure the public BTCUSDT market-data ticker exists first (no API key needed), so the
    // dashboard chart + live price work out of the box, independent of any connected trading
    // provider. See MarketDataSeedService.
    try {
      await this.marketDataSeedService.ensureSystemMarketDataTicker();
    } catch (err) {
      this.logger.error(`Market-data seed failed: ${err.message}`);
    }

    // Redis mode: the worker owns the Binance stream and publishes ticks; we just consume
    // them here (broadcast + evaluate/execute). No in-process sockets are opened, so a
    // backend restart never interrupts ingestion.
    if (this.liveStreamSource === 'redis') {
      this.subscribeToWorkerTicks();
      this.logger.log(`Live ticks sourced from worker via Redis '${MARKET_TICK_CHANNEL}'; in-process Binance streams disabled.`);
      return;
    }

    // Internal (legacy) mode: open in-process streams for any ticker that has real data
    // behind it (ACTIVE = live-strategy-managed, ONBOARDING = added but strategy generation
    // hasn't completed yet). INACTIVE means backfill failed; nothing to stream.
    const streamableTickers = await this.tickerRepo.find({ where: { status: Not(TickerStatus.INACTIVE) } });
    for (const ticker of streamableTickers) {
      this.startStream(ticker);
    }
  }

  /**
   * Subscribe to the worker's live-tick channel and drive the same handleTick path the
   * in-process stream used — but WITHOUT persisting (the worker already wrote the candle to
   * ohlcv_data; the backend is now purely a consumer for chart + evaluate/execute).
   */
  private subscribeToWorkerTicks() {
    this.redisSubscriber.subscribe(MARKET_TICK_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${MARKET_TICK_CHANNEL}: ${err.message}`);
      }
    });
    // When the worker owns execution it publishes position changes here; forward them to
    // browsers over Socket.IO (this process still owns the WS server).
    this.redisSubscriber.subscribe(POSITIONS_UPDATE_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${POSITIONS_UPDATE_CHANNEL}: ${err.message}`);
      }
    });
    // A bar closed in the worker -> the chart's entry/exit markers may have changed.
    this.redisSubscriber.subscribe(SIGNALS_UPDATE_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${SIGNALS_UPDATE_CHANNEL}: ${err.message}`);
      }
    });

    this.redisSubscriber.on('message', (channel, message) => {
      if (channel === POSITIONS_UPDATE_CHANNEL) {
        this.tradingGateway.broadcastPositionUpdate();
        return;
      }
      if (channel === SIGNALS_UPDATE_CHANNEL) {
        try {
          const { tickerId } = JSON.parse(message);
          if (tickerId) this.tradingGateway.broadcastSignalsUpdate(tickerId);
        } catch (e) {
          this.logger.error(`Failed to parse signals:update payload: ${e.message}`);
        }
        return;
      }
      if (channel !== MARKET_TICK_CHANNEL) return;
      let payload: any;
      try {
        payload = JSON.parse(message);
      } catch (e) {
        this.logger.error(`Failed to parse market:tick payload: ${e.message}`);
        return;
      }
      const tick: LiveKlineTick = {
        symbol: payload.symbol,
        interval: payload.interval,
        openTime: payload.openTime,
        closeTime: payload.closeTime,
        open: payload.open,
        high: payload.high,
        low: payload.low,
        close: payload.close,
        volume: payload.volume,
        isFinal: payload.isFinal,
      };
      // Fire-and-forget: a slow/failed tick must not stall the subscriber.
      this.handleTick(payload.tickerId, tick, { persist: false }).catch((err) =>
        this.logger.error(`handleTick failed for ticker ${payload.tickerId}: ${err.message}`),
      );
    });
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

  private async handleTick(
    tickerId: string,
    tick: LiveKlineTick,
    opts: { persist?: boolean } = {},
  ) {
    const persist = opts.persist !== false; // default true (in-process/legacy path)
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
    // every ~2s tick; upsert handles the case where it arrives more than once. Skipped when
    // the tick came from the worker (persist:false) — the worker is the writer of record.
    if (tick.isFinal && persist) {
      await this.marketDataService.upsertLiveCandle(tickerId, candle);
    }

    // Execution (mark price / hard exits / evaluate+execute) is owned entirely by the Python worker
    // (consolidation Phase B). The backend only ingests candles and fans ticks out to the frontend;
    // it never evaluates strategies or places orders.
  }
}
