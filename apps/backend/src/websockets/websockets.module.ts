import { Module } from '@nestjs/common';
import { TradingGateway } from './trading.gateway';
import { RedisAlertsBridgeService } from './redis-alerts-bridge.service';
import { RiskExecutionModule } from '../modules/risk-execution/risk-execution.module';

// API-process-only module (never imported by the worker — see worker.module.ts):
// a Socket.IO gateway has no meaningful server to bind to under
// NestFactory.createApplicationContext, since that bootstrap never starts an HTTP adapter.
@Module({
  imports: [RiskExecutionModule],
  providers: [TradingGateway, RedisAlertsBridgeService],
  exports: [TradingGateway],
})
export class WebsocketsModule {}
