import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { DailyLossTracking } from '../../entities/daily-loss-tracking.entity';
import { Position } from '../../entities/position.entity';
import { Order } from '../../entities/order.entity';
import { Ticker } from '../../entities/ticker.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { Alert } from '../../entities/alert.entity';
import { RiskGateService } from './risk-gate.service';
import { ExecutionService } from './execution.service';
import { RiskExecutionController } from './risk-execution.controller';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      RiskLimit,
      DailyLossTracking,
      Position,
      Order,
      Ticker,
      ProviderBalanceSnapshot,
      Alert,
    ]),
  ],
  providers: [RiskGateService, ExecutionService],
  controllers: [RiskExecutionController],
  exports: [RiskGateService, ExecutionService],
})
export class RiskExecutionModule {}
