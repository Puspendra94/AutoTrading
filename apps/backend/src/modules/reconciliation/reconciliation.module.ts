import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { ReconciliationReport } from '../../entities/reconciliation-report.entity';
import { Position } from '../../entities/position.entity';
import { Provider } from '../../entities/provider.entity';
import { ReconciliationService } from './reconciliation.service';
import { ProviderModule } from '../provider/provider.module';
import { NotificationModule } from '../notification/notification.module';

@Module({
  imports: [TypeOrmModule.forFeature([ReconciliationReport, Position, Provider]), ProviderModule, NotificationModule],
  providers: [ReconciliationService],
  exports: [ReconciliationService],
})
export class ReconciliationModule {}
