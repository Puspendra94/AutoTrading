import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Ticker } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { DataQualityFlag } from '../../entities/data-quality-flag.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
import { MarketDataService } from './market-data.service';
import { ProviderModule } from '../provider/provider.module';

// Deliberately minimal — no controller, no MarketStreamService, no WebsocketsModule.
// This is what the worker process imports (via JobsModule) for the data-quality sweep;
// live streaming lives in MarketStreamModule, imported only by app.module.ts (API
// process) — see that module's comment for why the split exists.
@Module({
  imports: [
    TypeOrmModule.forFeature([Ticker, OhlcvData, DataQualityFlag, Provider, MarketType]),
    ProviderModule,
  ],
  providers: [MarketDataService],
  exports: [MarketDataService],
})
export class MarketDataModule {}
