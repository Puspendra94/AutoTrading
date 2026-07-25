import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Ticker } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { DataQualityFlag } from '../../entities/data-quality-flag.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
import { MarketDataService } from './market-data.service';
import { HistoricalBackfillService } from './historical-backfill.service';
import { ProviderModule } from '../provider/provider.module';

// Deliberately minimal — no controller, no MarketStreamService, no WebsocketsModule.
// Provides plain MarketDataService (incl. the data-quality sweep) to consumers like
// StrategyModule and InternalJobsModule. Live streaming lives in MarketStreamModule.
// (The former separate Node worker process is retired — background jobs now run as
// InternalJobsModule endpoints, scheduled by the Python worker.)
@Module({
  imports: [
    TypeOrmModule.forFeature([Ticker, OhlcvData, DataQualityFlag, Provider, MarketType]),
    ProviderModule,
  ],
  providers: [MarketDataService, HistoricalBackfillService],
  exports: [MarketDataService],
})
export class MarketDataModule {}
