import { Injectable, Logger } from '@nestjs/common';
import { Cron, CronExpression } from '@nestjs/schedule';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Provider } from '../entities/provider.entity';
import { AllocationService } from '../modules/allocation/allocation.service';

@Injectable()
export class AllocationRebalanceJob {
  private readonly logger = new Logger(AllocationRebalanceJob.name);

  constructor(
    @InjectRepository(Provider) private readonly providerRepo: Repository<Provider>,
    private readonly allocationService: AllocationService,
  ) {}

  // Runs daily, alongside strategy re-evaluation (spec Section 6/10). Rebalancing only
  // changes the ceiling for new trades going forward — existing open positions ride
  // out under their prior allocation (spec 6).
  @Cron(CronExpression.EVERY_DAY_AT_2AM)
  async run() {
    const providers = await this.providerRepo.find();
    for (const provider of providers) {
      try {
        const snapshots = await this.allocationService.rebalanceProviderPool(provider.id);
        if (snapshots.length > 0) {
          this.logger.log(`Rebalanced ${snapshots.length} ticker(s) for provider ${provider.name}.`);
        }
      } catch (err) {
        this.logger.warn(`Allocation rebalance failed for provider ${provider.id}: ${err.message}`);
      }
    }
  }
}
