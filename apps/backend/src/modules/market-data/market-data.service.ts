import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { IsNull, LessThan, Repository } from 'typeorm';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { DataQualityFlag, FlagType } from '../../entities/data-quality-flag.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType, DEFAULT_MARKET_TYPE } from '../../entities/market-type.entity';
import { BinanceAdapter } from '../provider/adapters/binance.adapter';

@Injectable()
export class MarketDataService {
  private readonly logger = new Logger(MarketDataService.name);

  constructor(
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(OhlcvData)
    private readonly ohlcvRepo: Repository<OhlcvData>,
    @InjectRepository(DataQualityFlag)
    private readonly flagRepo: Repository<DataQualityFlag>,
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    @InjectRepository(MarketType)
    private readonly marketTypeRepo: Repository<MarketType>,
    private readonly binanceAdapter: BinanceAdapter,
  ) {}

  async listTickers(providerId?: string) {
    if (providerId) {
      return this.tickerRepo.find({ where: { providerId }, relations: ['provider', 'marketType'] });
    }
    return this.tickerRepo.find({ relations: ['provider', 'marketType'] });
  }

  async getTickerById(id: string) {
    const ticker = await this.tickerRepo.findOne({ where: { id }, relations: ['provider', 'marketType'] });
    if (!ticker) throw new NotFoundException('Ticker not found');
    return ticker;
  }

  async addTicker(providerId: string, symbol: string, interval = '1m') {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');

    let marketType = await this.marketTypeRepo.findOne({
      where: { providerId, name: DEFAULT_MARKET_TYPE },
    });
    if (!marketType) {
      marketType = this.marketTypeRepo.create({ providerId, name: DEFAULT_MARKET_TYPE });
      await this.marketTypeRepo.save(marketType);
    }

    const ticker = this.tickerRepo.create({
      providerId,
      marketTypeId: marketType.id,
      symbol: symbol.toUpperCase(),
      interval,
      status: TickerStatus.ONBOARDING,
      onboardingStage: OnboardingStage.FETCHING_HISTORY,
    });
    await this.tickerRepo.save(ticker);

    // Backfill + live-stream startup is orchestrated by MarketDataController (needs
    // MarketStreamService too, which would create a circular module dependency if
    // called from here) — see market-data.controller.ts.
    return ticker;
  }

  /**
   * Real historical backfill via Binance's public klines endpoint (no credentials
   * needed — market data is public). On failure, the ticker is marked FAILED and a
   * data-quality flag is written — no synthetic candles are ever inserted, per
   * CONVENTIONS.md's "no silent fallbacks to fake data" rule.
   */
  async backfillHistoricalData(tickerId: string, limit = 1000) {
    const ticker = await this.getTickerById(tickerId);
    ticker.onboardingStage = OnboardingStage.FETCHING_HISTORY;
    await this.tickerRepo.save(ticker);

    let candles;
    try {
      candles = await this.binanceAdapter.getPublicKlines(ticker.symbol, ticker.interval, limit);
    } catch (err) {
      this.logger.error(`Binance klines fetch failed for ${ticker.symbol}: ${err.message}`);
      ticker.onboardingStage = OnboardingStage.FAILED;
      ticker.status = TickerStatus.INACTIVE;
      await this.tickerRepo.save(ticker);
      await this.flagRepo.save(
        this.flagRepo.create({
          tickerId: ticker.id,
          flagType: FlagType.GAP,
          detailJson: { reason: 'historical_backfill_failed', error: err.message },
        }),
      );
      return { tickerId: ticker.id, candlesFetched: 0, failed: true, error: err.message };
    }

    if (candles.length === 0) {
      ticker.onboardingStage = OnboardingStage.FAILED;
      ticker.status = TickerStatus.INACTIVE;
      await this.tickerRepo.save(ticker);
      await this.flagRepo.save(
        this.flagRepo.create({
          tickerId: ticker.id,
          flagType: FlagType.GAP,
          detailJson: { reason: 'no_historical_data_returned' },
        }),
      );
      return { tickerId: ticker.id, candlesFetched: 0, failed: true };
    }

    // Duplicate-timestamp check within this single fetch batch (spec 4.7) — a genuine
    // upstream data-quality issue, distinct from the harmless unique-key conflict that
    // happens on every re-backfill of an already-stored range (still safely ignored
    // below, since that's expected, not a quality problem).
    const seenTimestamps = new Map<number, number>();
    for (const candle of candles) {
      const t = new Date(candle.timestamp).getTime();
      seenTimestamps.set(t, (seenTimestamps.get(t) || 0) + 1);
    }
    const duplicateTimestamps = [...seenTimestamps.entries()].filter(([, count]) => count > 1);
    if (duplicateTimestamps.length > 0) {
      await this.flagRepo.save(
        this.flagRepo.create({
          tickerId: ticker.id,
          flagType: FlagType.DUPLICATE,
          detailJson: { duplicateCount: duplicateTimestamps.length, timestamps: duplicateTimestamps.map(([t]) => new Date(t)) },
        }),
      );
    }

    for (const candle of candles) {
      const entity = this.ohlcvRepo.create({ tickerId: ticker.id, ...candle });
      await this.ohlcvRepo.save(entity).catch(() => {}); // duplicate primary key on re-backfill, safe to ignore
    }

    await this.auditDataQuality(ticker.id);

    return { tickerId: ticker.id, candlesFetched: candles.length, failed: false };
  }

  /** Parses '1m'/'5m'/'15m'/'1h'/'4h'/'1d'-style interval strings into milliseconds —
   * used to make gap detection and timestamp-alignment checks interval-aware instead of
   * a fixed constant that only happened to fit 1-minute tickers. */
  private intervalToMs(interval: string): number {
    const match = interval.match(/^(\d+)([mhdw])$/i);
    if (!match) return 60 * 1000; // unrecognized — fall back to 1m rather than throwing
    const value = parseInt(match[1], 10);
    const unitMs: Record<string, number> = { m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };
    return value * (unitMs[match[2].toLowerCase()] || 60_000);
  }

  async auditDataQuality(tickerId: string) {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    const intervalMs = this.intervalToMs(ticker?.interval || '1m');

    const candles = await this.ohlcvRepo.find({
      where: { tickerId },
      order: { timestamp: 'ASC' },
      take: 1000,
    });

    if (candles.length < 2) return;

    for (let i = 1; i < candles.length; i++) {
      const prev = candles[i - 1];
      const curr = candles[i];
      const timeDiff = curr.timestamp.getTime() - prev.timestamp.getTime();

      // Gap threshold scales with the ticker's own interval (previously a fixed 3
      // minutes regardless of interval, which false-positived on anything slower than
      // ~1m and missed real gaps on anything slower than 3m).
      if (timeDiff > intervalMs * 2) {
        await this.flagRepo.save(
          this.flagRepo.create({
            tickerId,
            flagType: FlagType.GAP,
            detailJson: { gapMs: timeDiff, from: prev.timestamp, to: curr.timestamp },
          }),
        );
      }

      const priceChangePct = Math.abs(Number(curr.close) - Number(prev.close)) / Number(prev.close);
      if (priceChangePct > 0.1) {
        await this.flagRepo.save(
          this.flagRepo.create({
            tickerId,
            flagType: FlagType.SPIKE,
            detailJson: { changePct: priceChangePct * 100, price: curr.close, prevPrice: prev.close },
          }),
        );
      }

      // Timezone/DST inconsistency (spec 4.7) — every candle's timestamp should land
      // exactly on its interval's boundary (e.g. a 1m candle at :00 seconds/ms); a
      // misaligned timestamp is the signature of a timezone-conversion bug upstream.
      if (curr.timestamp.getTime() % intervalMs !== 0) {
        await this.flagRepo.save(
          this.flagRepo.create({
            tickerId,
            flagType: FlagType.TZ_MISMATCH,
            detailJson: { timestamp: curr.timestamp, intervalMs, remainderMs: curr.timestamp.getTime() % intervalMs },
          }),
        );
      }
    }
  }

  /** Spec 4.7 — "strategy generation and backtesting should refuse to proceed... if the
   * underlying data has unresolved quality flags." Only GAP and SPIKE are treated as
   * generation-blocking here; DUPLICATE/TZ_MISMATCH alone don't necessarily invalidate
   * the closes used for backtesting, so they're recorded but non-blocking. */
  async hasUnresolvedBlockingFlags(tickerId: string): Promise<boolean> {
    const count = await this.flagRepo.count({
      where: [
        { tickerId, flagType: FlagType.GAP, resolvedAt: IsNull() },
        { tickerId, flagType: FlagType.SPIKE, resolvedAt: IsNull() },
      ],
    });
    return count > 0;
  }

  async getCandles(tickerId: string, limit = 200, before?: number) {
    return this.ohlcvRepo
      .find({
        // `before` (epoch ms) pages older history for chart scroll-back: fetch the newest
        // `limit` candles strictly older than the cursor. Omitted -> most recent window.
        where: before ? { tickerId, timestamp: LessThan(new Date(before)) } : { tickerId },
        order: { timestamp: 'DESC' },
        take: limit,
      })
      .then((res) => res.reverse());
  }

  private static readonly PG_UNIT: Record<string, string> = { m: 'minutes', h: 'hours', d: 'days', w: 'weeks' };

  /**
   * Candles rolled up to an arbitrary interval from the stored 1m base via TimescaleDB
   * time_bucket (first open, max high, min low, last close, sum volume). We only ingest 1m
   * (the minimum interval), so every higher chart timeframe is derived in the DB — cheap,
   * and no duplicate ingestion. `1m` short-circuits to the raw rows.
   */
  async getCandlesForInterval(tickerId: string, interval = '1m', limit = 500, before?: number) {
    if (interval === '1m') return this.getCandles(tickerId, limit, before);
    const match = interval.match(/^(\d+)([mhdw])$/i);
    if (!match) throw new BadRequestException(`Unsupported interval: ${interval}`);
    const bucket = `${parseInt(match[1], 10)} ${MarketDataService.PG_UNIT[match[2].toLowerCase()]}`;
    // `before` (epoch ms) pages older buckets for chart scroll-back; omitted -> latest window.
    // bucket is regex-validated -> safe to inline as an interval literal.
    const rows = await this.ohlcvRepo.query(
      `SELECT "timestamp", open, high, low, close, volume FROM (
         SELECT time_bucket(INTERVAL '${bucket}', "timestamp") AS "timestamp",
           first(open, "timestamp") AS open, max(high) AS high, min(low) AS low,
           last(close, "timestamp") AS close, sum(volume) AS volume
         FROM "Algo_Trading"."ohlcv_data"
         WHERE ticker_id = $1 ${before ? 'AND "timestamp" < $3' : ''}
         GROUP BY 1 ORDER BY 1 DESC LIMIT $2
       ) t ORDER BY "timestamp" ASC`,
      before ? [tickerId, limit, new Date(before)] : [tickerId, limit],
    );
    return rows;
  }

  async upsertLiveCandle(tickerId: string, candle: { timestamp: Date; open: number; high: number; low: number; close: number; volume: number }) {
    // Atomic upsert on the (ticker_id, timestamp) unique key. The previous find-then-insert
    // raced with any concurrent writer of the same candle — notably the Python worker's 1m
    // archive backfill, which overlaps the live stream's current minute — throwing a
    // duplicate-key error that, unhandled in the tick handler, crashed the process.
    await this.ohlcvRepo.upsert({ tickerId, ...candle }, ['tickerId', 'timestamp']);
  }
}
