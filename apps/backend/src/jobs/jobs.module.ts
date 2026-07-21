import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Provider } from '../entities/provider.entity';
import { Ticker } from '../entities/ticker.entity';
import { Alert } from '../entities/alert.entity';
import { ProviderModule } from '../modules/provider/provider.module';
import { MarketDataModule } from '../modules/market-data/market-data.module';
import { StrategyModule } from '../modules/strategy/strategy.module';
import { AllocationModule } from '../modules/allocation/allocation.module';
import { ReconciliationModule } from '../modules/reconciliation/reconciliation.module';
import { NotificationModule } from '../modules/notification/notification.module';
import { BalanceSyncJob } from './balance-sync.job';
import { ReconciliationJob } from './reconciliation.job';
import { StrategyReevaluationJob } from './strategy-reevaluation.job';
import { AllocationRebalanceJob } from './allocation-rebalance.job';
import { DataQualityMonitorJob } from './data-quality-monitor.job';
import { AlertDigestJob } from './alert-digest.job';

// All five spec-Section-10 background jobs, run only inside the worker process
// (worker.module.ts) — never imported by app.module.ts, so the API process stays free
// of scheduled work (spec 3's "never blocks the live order path" architectural rule).
@Module({
  imports: [
    TypeOrmModule.forFeature([Provider, Ticker, Alert]),
    ProviderModule,
    MarketDataModule,
    StrategyModule,
    AllocationModule,
    ReconciliationModule,
    NotificationModule,
  ],
  providers: [
    BalanceSyncJob,
    ReconciliationJob,
    StrategyReevaluationJob,
    AllocationRebalanceJob,
    DataQualityMonitorJob,
    AlertDigestJob,
  ],
})
export class JobsModule {}
