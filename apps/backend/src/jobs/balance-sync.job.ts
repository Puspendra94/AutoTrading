import { Injectable, Logger } from '@nestjs/common';
import { Interval } from '@nestjs/schedule';
import { ProviderService } from '../modules/provider/provider.service';

const INTERVAL_MS = (parseInt(process.env.BALANCE_SYNC_INTERVAL_MINUTES || '5', 10)) * 60 * 1000;

@Injectable()
export class BalanceSyncJob {
  private readonly logger = new Logger(BalanceSyncJob.name);

  constructor(private readonly providerService: ProviderService) {}

  @Interval(INTERVAL_MS)
  async run() {
    const providers = await this.providerService.listProvidersWithCredentials();
    for (const provider of providers) {
      try {
        await this.providerService.syncBalance(provider.id);
      } catch (err) {
        this.logger.warn(`Balance sync failed for provider ${provider.id} (${provider.name}): ${err.message}`);
      }
    }
    if (providers.length > 0) {
      this.logger.log(`Balance sync completed for ${providers.length} provider(s).`);
    }
  }
}
