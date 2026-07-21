import { Injectable, Logger } from '@nestjs/common';
import { Cron, CronExpression } from '@nestjs/schedule';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Ticker, TickerStatus } from '../entities/ticker.entity';
import { MarketDataService } from '../modules/market-data/market-data.service';

@Injectable()
export class DataQualityMonitorJob {
  private readonly logger = new Logger(DataQualityMonitorJob.name);

  constructor(
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    private readonly marketDataService: MarketDataService,
  ) {}

  // On top of the on-ingestion checks already run per-candle (spec 4.7) — a scheduled
  // sweep catches anything that accumulated without a fresh ingestion event triggering it.
  @Cron(CronExpression.EVERY_6_HOURS)
  async run() {
    const tickers = await this.tickerRepo.find({ where: { status: TickerStatus.ACTIVE } });
    for (const ticker of tickers) {
      try {
        await this.marketDataService.auditDataQuality(ticker.id);
      } catch (err) {
        this.logger.warn(`Data quality sweep failed for ticker ${ticker.id}: ${err.message}`);
      }
    }
  }
}
