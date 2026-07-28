import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Position } from '../../entities/position.entity';
import { Ticker } from '../../entities/ticker.entity';
import { User } from '../../entities/user.entity';
import { Provider } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
import { MarketDataModule } from './market-data.module';
import { MarketStreamService } from './market-stream.service';
import { MarketDataSeedService } from './market-data-seed.service';
import { MarketDataController } from './market-data.controller';
import { ProviderModule } from '../provider/provider.module';
import { WebsocketsModule } from '../../websockets/websockets.module';

// Live streaming: MarketStreamService.onModuleInit() opens live Binance WebSocket streams
// and pushes through TradingGateway. Imported by app.module.ts (the API process, which has
// the Socket.IO server bound). MarketDataModule (plain MarketDataService, no streaming) is
// the lighter dependency used by strategy/internal-jobs consumers.
@Module({
  imports: [
    TypeOrmModule.forFeature([Position, Ticker, User, Provider, MarketType]),
    MarketDataModule,
    ProviderModule,
    WebsocketsModule,
  ],
  providers: [MarketStreamService, MarketDataSeedService],
  controllers: [MarketDataController],
  exports: [MarketStreamService],
})
export class MarketStreamModule {}
