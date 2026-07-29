import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { dataSourceOptions } from './database/data-source';
import { AuthModule } from './modules/auth/auth.module';
import { ProviderModule } from './modules/provider/provider.module';
import { MarketStreamModule } from './modules/market-data/market-stream.module';
import { StrategyModule } from './modules/strategy/strategy.module';
import { PatternModule } from './modules/pattern/pattern.module';
import { RiskExecutionModule } from './modules/risk-execution/risk-execution.module';
import { AllocationModule } from './modules/allocation/allocation.module';
import { ReconciliationModule } from './modules/reconciliation/reconciliation.module';
import { NotificationModule } from './modules/notification/notification.module';
import { LlmModule } from './modules/llm/llm.module';
import { InternalJobsModule } from './modules/internal-jobs/internal-jobs.module';
import { WebsocketsModule } from './websockets/websockets.module';
import { RedisModule } from './common/redis/redis.module';

@Module({
  imports: [
    // Environment is loaded and validated by config/configuration.ts (imported wherever
    // config is read, including database/data-source.ts below) — no ConfigModule needed.
    TypeOrmModule.forRoot(dataSourceOptions),
    RedisModule,
    AuthModule,
    ProviderModule,
    MarketStreamModule,
    StrategyModule,
    PatternModule,
    RiskExecutionModule,
    AllocationModule,
    ReconciliationModule,
    NotificationModule,
    LlmModule,
    InternalJobsModule,
    WebsocketsModule,
  ],
})
export class AppModule {}
