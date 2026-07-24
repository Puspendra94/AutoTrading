import { Controller, Post, UseGuards, Logger } from '@nestjs/common';
import { InternalKeyGuard } from '../../common/guards/internal-key.guard';
import { InternalJobsService } from './internal-jobs.service';

// Scheduled-job triggers for the Python worker (worker-py). Guarded by a shared secret
// (see InternalKeyGuard); never exposed to browsers. One endpoint per former cron job,
// each reusing the same service the retired Node worker's job called.
@Controller('internal/jobs')
@UseGuards(InternalKeyGuard)
export class InternalJobsController {
  private readonly logger = new Logger(InternalJobsController.name);

  constructor(private readonly jobs: InternalJobsService) {}

  private async run(name: string, fn: () => Promise<any>) {
    const result = await fn();
    this.logger.log(`Job '${name}' complete: ${JSON.stringify(result)}`);
    return { job: name, ...result };
  }

  @Post('balance-sync')
  balanceSync() {
    return this.run('balance-sync', () => this.jobs.balanceSync());
  }

  @Post('reconciliation')
  reconciliation() {
    return this.run('reconciliation', () => this.jobs.reconciliation());
  }

  @Post('allocation-rebalance')
  allocationRebalance() {
    return this.run('allocation-rebalance', () => this.jobs.allocationRebalance());
  }

  @Post('alert-digest')
  alertDigest() {
    return this.run('alert-digest', () => this.jobs.alertDigest());
  }

  @Post('strategy-reevaluation')
  strategyReevaluation() {
    return this.run('strategy-reevaluation', () => this.jobs.strategyReevaluation());
  }

  @Post('data-quality')
  dataQuality() {
    return this.run('data-quality', () => this.jobs.dataQuality());
  }
}
