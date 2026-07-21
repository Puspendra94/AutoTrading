import { Injectable, Logger } from '@nestjs/common';
import { Cron, CronExpression } from '@nestjs/schedule';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Provider } from '../entities/provider.entity';
import { ReconciliationService } from '../modules/reconciliation/reconciliation.service';

@Injectable()
export class ReconciliationJob {
  private readonly logger = new Logger(ReconciliationJob.name);

  constructor(
    @InjectRepository(Provider) private readonly providerRepo: Repository<Provider>,
    private readonly reconciliationService: ReconciliationService,
  ) {}

  @Cron(CronExpression.EVERY_HOUR)
  async run() {
    const providers = await this.providerRepo.find();
    for (const provider of providers) {
      try {
        await this.reconciliationService.runReconciliationAudit(provider.id);
      } catch (err) {
        this.logger.warn(`Reconciliation failed for provider ${provider.id}: ${err.message}`);
      }
    }
  }
}
