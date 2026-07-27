import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { BinanceAdapter } from '../provider/adapters/binance.adapter';
import { config } from '../../config/configuration';

// Self-paced background pipeline that walks each live ticker's 1m history backward toward
// the symbol's listing date ("since the beginning"), then keeps recent gaps patched. Runs
// in-process on a recursive timer with no overlap, throttled to stay well under Binance's
// public rate limits. Resumable across restarts: every tick re-derives the earliest stored
// candle from the DB, so it simply continues from wherever it left off.
const TICK_INTERVAL_MS = 8000; // gap between ticks
const BATCHES_PER_TICK = 8; // klines requests per ticker per tick
const BATCH_SIZE = 1000; // candles per request (Binance max)
const BATCH_DELAY_MS = 300; // pause between requests within a tick
const HARD_FLOOR_MS = Date.UTC(2017, 0, 1); // never page older than 2017-01-01 (pre-Binance)
const BOOT_DELAY_MS = 15000; // let the app finish booting before the first tick

@Injectable()
export class HistoricalBackfillService implements OnModuleInit {
  private readonly logger = new Logger(HistoricalBackfillService.name);
  private readonly enabled = config.historicalBackfillEnabled;
  private readonly backwardDone = new Set<string>(); // tickerIds fully backfilled to genesis
  private running = false;
  private tickCount = 0;

  constructor(
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(OhlcvData) private readonly ohlcvRepo: Repository<OhlcvData>,
    private readonly binanceAdapter: BinanceAdapter,
  ) {}

  onModuleInit() {
    if (!this.enabled) {
      this.logger.log('Historical backfill pipeline disabled (HISTORICAL_BACKFILL_ENABLED=false).');
      return;
    }
    setTimeout(() => this.loop(), BOOT_DELAY_MS);
  }

  private loop() {
    if (!this.enabled) return;
    this.tick()
      .catch((e) => this.logger.error(`Backfill tick failed: ${e.message}`))
      .finally(() => setTimeout(() => this.loop(), TICK_INTERVAL_MS));
  }

  private async tick() {
    if (this.running) return; // never overlap
    this.running = true;
    this.tickCount++;
    try {
      const tickers = (await this.tickerRepo.find({ where: { interval: '1m' } })).filter(
        (t) => t.status !== TickerStatus.INACTIVE,
      );
      for (const t of tickers) {
        if (!this.backwardDone.has(t.id)) {
          await this.backwardChunk(t);
        } else if (this.tickCount % 10 === 0) {
          await this.forwardGapFill(t); // occasional patch of recent minutes
        }
      }
    } finally {
      this.running = false;
    }
  }

  /** Pages backward from the earliest stored candle toward the listing date, a bounded
   * number of batches per tick. Marks the ticker done when Binance stops returning older
   * candles (short/empty batch, non-advancing edge, or hard floor reached). */
  private async backwardChunk(ticker: Ticker) {
    let earliest = await this.earliestTimestamp(ticker.id);
    for (let b = 0; b < BATCHES_PER_TICK; b++) {
      const endTime = (earliest ? earliest.getTime() : Date.now()) - 1;
      if (endTime <= HARD_FLOOR_MS) {
        this.markDone(ticker, 'reached hard floor');
        return;
      }
      let candles;
      try {
        candles = await this.binanceAdapter.getPublicKlines(ticker.symbol, ticker.interval, BATCH_SIZE, { endTime });
      } catch (e: any) {
        this.logger.warn(`Backfill fetch failed for ${ticker.symbol}: ${e.message}`);
        return; // retry next tick
      }
      if (!candles.length) {
        this.markDone(ticker, 'no older candles');
        return;
      }

      await this.ohlcvRepo
        .upsert(candles.map((c) => ({ tickerId: ticker.id, ...c })), ['tickerId', 'timestamp'])
        .catch((e) => this.logger.warn(`Backfill upsert failed for ${ticker.symbol}: ${e.message}`));

      const oldest = candles.reduce((min, c) => Math.min(min, c.timestamp.getTime()), candles[0].timestamp.getTime());
      if (earliest && oldest >= earliest.getTime()) {
        this.markDone(ticker, 'edge no longer advancing'); // reached the listing start
        return;
      }
      earliest = new Date(oldest);
      if (candles.length < BATCH_SIZE) {
        this.markDone(ticker, 'short batch');
        return;
      }
      await this.sleep(BATCH_DELAY_MS);
    }
    this.logger.log(`Backfill ${ticker.symbol}: reached ${earliest?.toISOString()} (still walking back)`);
  }

  /** Pull the most recent window and upsert it, patching any minute the live stream missed. */
  private async forwardGapFill(ticker: Ticker) {
    try {
      const candles = await this.binanceAdapter.getPublicKlines(ticker.symbol, ticker.interval, BATCH_SIZE);
      if (candles.length) {
        await this.ohlcvRepo.upsert(candles.map((c) => ({ tickerId: ticker.id, ...c })), ['tickerId', 'timestamp']);
      }
    } catch (e: any) {
      this.logger.warn(`Forward gap-fill failed for ${ticker.symbol}: ${e.message}`);
    }
  }

  private markDone(ticker: Ticker, why: string) {
    if (!this.backwardDone.has(ticker.id)) {
      this.backwardDone.add(ticker.id);
      this.logger.log(`Backfill ${ticker.symbol} (${ticker.id}) complete to genesis — ${why}.`);
    }
  }

  private async earliestTimestamp(tickerId: string): Promise<Date | null> {
    const row = await this.ohlcvRepo.findOne({
      where: { tickerId },
      order: { timestamp: 'ASC' },
      select: ['timestamp'],
    });
    return row?.timestamp ?? null;
  }

  private sleep(ms: number) {
    return new Promise((r) => setTimeout(r, ms));
  }
}
