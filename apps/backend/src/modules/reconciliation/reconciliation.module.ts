import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { ReconciliationReport } from '../../entities/reconciliation-report.entity';
import { Position } from '../../entities/position.entity';
import { Provider } from '../../entities/provider.entity';
import { Alert } from '../../entities/alert.entity';
import { ReconciliationService } from './reconciliation.service';

@Module({
  imports: [TypeOrmModule.forFeature([ReconciliationReport, Position, Provider, Alert])],
  providers: [ReconciliationService],
  exports: [ReconciliationService],
})
export class ReconciliationModule {}
