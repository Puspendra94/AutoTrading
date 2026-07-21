import { Module, OnModuleInit } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { TypeOrmModule } from '@nestjs/typeorm';
import { dataSourceOptions } from './database/data-source';
import { AuthModule } from './modules/auth/auth.module';
import { ProviderModule } from './modules/provider/provider.module';
import { MarketStreamModule } from './modules/market-data/market-stream.module';
import { StrategyModule } from './modules/strategy/strategy.module';
import { RiskExecutionModule } from './modules/risk-execution/risk-execution.module';
import { AllocationModule } from './modules/allocation/allocation.module';
import { ReconciliationModule } from './modules/reconciliation/reconciliation.module';
import { NotificationModule } from './modules/notification/notification.module';
import { LlmModule } from './modules/llm/llm.module';
import { WebsocketsModule } from './websockets/websockets.module';
import { RedisModule } from './common/redis/redis.module';
import dataSource from './database/data-source';

@Module({
  imports: [
    ConfigModule.forRoot({ isGlobal: true }),
    TypeOrmModule.forRoot(dataSourceOptions),
    RedisModule,
    AuthModule,
    ProviderModule,
    MarketStreamModule,
    StrategyModule,
    RiskExecutionModule,
    AllocationModule,
    ReconciliationModule,
    NotificationModule,
    LlmModule,
    WebsocketsModule,
  ],
})
export class AppModule implements OnModuleInit {
  async onModuleInit() {
    // Run TypeORM schema migrations automatically on startup (Section 14.1)
    try {
      if (!dataSource.isInitialized) {
        await dataSource.initialize();
      }
      console.log('Running database migrations...');
      await dataSource.runMigrations();
      console.log('Database migrations completed successfully.');
    } catch (err) {
      console.warn('Migration auto-run warning:', err.message);
    }
  }
}
