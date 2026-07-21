import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Ticker } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { DataQualityFlag } from '../../entities/data-quality-flag.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
import { MarketDataService } from './market-data.service';
import { MarketDataController } from './market-data.controller';

@Module({
  imports: [
    TypeOrmModule.forFeature([Ticker, OhlcvData, DataQualityFlag, Provider, MarketType]),
  ],
  providers: [MarketDataService],
  controllers: [MarketDataController],
  exports: [MarketDataService],
})
export class MarketDataModule {}
