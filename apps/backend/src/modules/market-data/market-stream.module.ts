import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Position } from '../../entities/position.entity';
import { Ticker } from '../../entities/ticker.entity';
import { MarketDataModule } from './market-data.module';
import { MarketStreamService } from './market-stream.service';
import { MarketDataController } from './market-data.controller';
import { ProviderModule } from '../provider/provider.module';
import { WebsocketsModule } from '../../websockets/websockets.module';

// API-process-only (imported by app.module.ts, never by worker.module.ts/JobsModule):
// MarketStreamService.onModuleInit() opens live Binance WebSocket streams and pushes
// through TradingGateway, which has no real Socket.IO server to bind to under the
// worker's createApplicationContext bootstrap (no HTTP adapter) — importing this in
// the worker crashes it the moment the first tick arrives. MarketDataModule (plain
// MarketDataService, no streaming) is what the worker uses instead.
@Module({
  imports: [TypeOrmModule.forFeature([Position, Ticker]), MarketDataModule, ProviderModule, WebsocketsModule],
  providers: [MarketStreamService],
  controllers: [MarketDataController],
  exports: [MarketStreamService],
})
export class MarketStreamModule {}
