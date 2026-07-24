import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Provider } from '../../entities/provider.entity';
import { Ticker } from '../../entities/ticker.entity';
import { Alert } from '../../entities/alert.entity';
import { ProviderModule } from '../provider/provider.module';
import { ReconciliationModule } from '../reconciliation/reconciliation.module';
import { AllocationModule } from '../allocation/allocation.module';
import { NotificationModule } from '../notification/notification.module';
import { MarketDataModule } from '../market-data/market-data.module';
import { StrategyModule } from '../strategy/strategy.module';
import { InternalJobsService } from './internal-jobs.service';
import { InternalJobsController } from './internal-jobs.controller';

// API-process module hosting the former worker jobs as internal, secret-guarded endpoints
// the Python worker triggers on a schedule. Reuses the exact services the old jobs called.
@Module({
  imports: [
    TypeOrmModule.forFeature([Provider, Ticker, Alert]),
    ProviderModule,
    ReconciliationModule,
    AllocationModule,
    NotificationModule,
    MarketDataModule,
    StrategyModule,
  ],
  providers: [InternalJobsService],
  controllers: [InternalJobsController],
})
export class InternalJobsModule {}
