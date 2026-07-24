import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { DailyLossTracking } from '../../entities/daily-loss-tracking.entity';
import { Position } from '../../entities/position.entity';
import { Order } from '../../entities/order.entity';
import { Ticker } from '../../entities/ticker.entity';
import { Provider } from '../../entities/provider.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { AllocationSnapshot } from '../../entities/allocation-snapshot.entity';
import { Strategy } from '../../entities/strategy.entity';
import { Alert } from '../../entities/alert.entity';
import { RiskGateService } from './risk-gate.service';
import { ExecutionService } from './execution.service';
import { RiskExecutionController } from './risk-execution.controller';
import { ProviderModule } from '../provider/provider.module';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      RiskLimit,
      DailyLossTracking,
      Position,
      Order,
      Ticker,
      Provider,
      ProviderBalanceSnapshot,
      ProviderSchedule,
      AllocationSnapshot,
      Strategy,
      Alert,
    ]),
    ProviderModule,
  ],
  providers: [RiskGateService, ExecutionService],
  controllers: [RiskExecutionController],
  exports: [RiskGateService, ExecutionService],
})
export class RiskExecutionModule {}
