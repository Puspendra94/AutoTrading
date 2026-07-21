import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { DataQualityFlag, FlagType } from '../../entities/data-quality-flag.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
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

    let marketType = await this.marketTypeRepo.findOne({ where: { providerId, name: 'spot' } });
    if (!marketType) {
      marketType = this.marketTypeRepo.create({ providerId, name: 'spot' });
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

    for (const candle of candles) {
      const entity = this.ohlcvRepo.create({ tickerId: ticker.id, ...candle });
      await this.ohlcvRepo.save(entity).catch(() => {}); // duplicate primary key on re-backfill, safe to ignore
    }

    await this.auditDataQuality(ticker.id);

    return { tickerId: ticker.id, candlesFetched: candles.length, failed: false };
  }

  async auditDataQuality(tickerId: string) {
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

      if (timeDiff > 3 * 60 * 1000) {
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
    }
  }

  async getCandles(tickerId: string, limit = 200) {
    return this.ohlcvRepo
      .find({
        where: { tickerId },
        order: { timestamp: 'DESC' },
        take: limit,
      })
      .then((res) => res.reverse());
  }

  async upsertLiveCandle(tickerId: string, candle: { timestamp: Date; open: number; high: number; low: number; close: number; volume: number }) {
    const existing = await this.ohlcvRepo.findOne({ where: { tickerId, timestamp: candle.timestamp } });
    if (existing) {
      await this.ohlcvRepo.update({ tickerId, timestamp: candle.timestamp }, candle);
    } else {
      await this.ohlcvRepo.save(this.ohlcvRepo.create({ tickerId, ...candle }));
    }
  }
}
