import { Module } from '@nestjs/common';
import { TradingGateway } from './trading.gateway';
import { RedisAlertsBridgeService } from './redis-alerts-bridge.service';
import { RedisGenerationBridgeService } from './redis-generation-bridge.service';
import { RiskExecutionModule } from '../modules/risk-execution/risk-execution.module';

// Socket.IO gateway module — belongs to the API process (app.module.ts), which starts the
// HTTP adapter the gateway binds its server to.
@Module({
  imports: [RiskExecutionModule],
  providers: [TradingGateway, RedisAlertsBridgeService, RedisGenerationBridgeService],
  exports: [TradingGateway],
})
export class WebsocketsModule {}
