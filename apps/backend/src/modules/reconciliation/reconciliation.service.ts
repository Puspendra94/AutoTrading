import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { ReconciliationReport, ReconciliationStatus } from '../../entities/reconciliation-report.entity';
import { Position, PositionStatus } from '../../entities/position.entity';
import { Provider } from '../../entities/provider.entity';
import { Alert, AlertSeverity } from '../../entities/alert.entity';

@Injectable()
export class ReconciliationService {
  constructor(
    @InjectRepository(ReconciliationReport)
    private readonly reportRepo: Repository<ReconciliationReport>,
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    @InjectRepository(Alert)
    private readonly alertRepo: Repository<Alert>,
  ) {}

  async runReconciliationAudit(providerId: string) {
    const openPositions = await this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker'],
    });

    const mismatches = [];
    // Simulated audit check against exchange API
    const report = this.reportRepo.create({
      providerId,
      mismatchesFound: mismatches.length,
      detailJson: { auditedPositionsCount: openPositions.length, mismatches },
      status: mismatches.length === 0 ? ReconciliationStatus.CLEAN : ReconciliationStatus.MISMATCH,
    });
    await this.reportRepo.save(report);

    if (mismatches.length > 0) {
      await this.alertRepo.save(
        this.alertRepo.create({
          severity: AlertSeverity.WARNING,
          category: 'RECONCILIATION_MISMATCH',
          message: `Reconciliation audit found ${mismatches.length} mismatch(es) for provider ${providerId}.`,
          relatedEntityType: 'ReconciliationReport',
          relatedEntityId: report.id,
        }),
      );
    }

    return report;
  }
}
