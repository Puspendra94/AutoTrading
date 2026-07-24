import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Provider } from '../../entities/provider.entity';
import { ProviderCredential } from '../../entities/provider-credential.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { ProviderService } from './provider.service';
import { ProviderController } from './provider.controller';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { BinanceAdapter } from './adapters/binance.adapter';
import { NotificationModule } from '../notification/notification.module';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      Provider,
      ProviderCredential,
      ProviderBalanceSnapshot,
      ProviderSchedule,
      RiskLimit,
    ]),
    NotificationModule,
  ],
  providers: [ProviderService, PlaintextSecretsProvider, BinanceAdapter],
  controllers: [ProviderController],
  exports: [ProviderService, PlaintextSecretsProvider, BinanceAdapter],
})
export class ProviderModule {}
