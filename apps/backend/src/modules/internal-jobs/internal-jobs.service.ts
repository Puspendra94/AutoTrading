import { Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { MoreThan, Repository } from 'typeorm';
import { Provider } from '../../entities/provider.entity';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { Alert, AlertSeverity } from '../../entities/alert.entity';
import { ProviderService } from '../provider/provider.service';
import { ReconciliationService } from '../reconciliation/reconciliation.service';
import { AllocationService } from '../allocation/allocation.service';
import { NotificationService } from '../notification/notification.service';
import { MarketDataService } from '../market-data/market-data.service';
import { StrategyEngineService } from '../strategy/strategy-engine.service';

/**
 * The orchestration bodies that used to live in apps/backend/src/jobs/*.job.ts. They now
 * run in the API process, triggered on a schedule by the Python worker (worker-py) through
 * the guarded /internal/jobs/* endpoints — so there's a single 24/7 scheduler (Python) and
 * a single implementation of every job (reusing the proven services here, no duplication).
 */
@Injectable()
export class InternalJobsService {
  private readonly logger = new Logger(InternalJobsService.name);
  private lastDigestAt = new Date();

  constructor(
    @InjectRepository(Provider) private readonly providerRepo: Repository<Provider>,
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(Alert) private readonly alertRepo: Repository<Alert>,
    private readonly providerService: ProviderService,
    private readonly reconciliationService: ReconciliationService,
    private readonly allocationService: AllocationService,
    private readonly notificationService: NotificationService,
    private readonly marketDataService: MarketDataService,
    private readonly strategyEngineService: StrategyEngineService,
  ) {}

  // Was BalanceSyncJob (@Interval, default 5 min).
  async balanceSync() {
    const providers = await this.providerService.listProvidersWithCredentials();
    let synced = 0;
    for (const provider of providers) {
      try {
        await this.providerService.syncBalance(provider.id);
        synced++;
      } catch (err) {
        this.logger.warn(`Balance sync failed for provider ${provider.id} (${provider.name}): ${err.message}`);
      }
    }
    return { providers: providers.length, synced };
  }

  // Was ReconciliationJob (@Cron EVERY_HOUR).
  async reconciliation() {
    const providers = await this.providerRepo.find();
    let ok = 0;
    for (const provider of providers) {
      try {
        await this.reconciliationService.runReconciliationAudit(provider.id);
        ok++;
      } catch (err) {
        this.logger.warn(`Reconciliation failed for provider ${provider.id}: ${err.message}`);
      }
    }
    return { providers: providers.length, reconciled: ok };
  }

  // Was AllocationRebalanceJob (@Cron EVERY_DAY_AT_2AM).
  async allocationRebalance() {
    const providers = await this.providerRepo.find();
    let rebalanced = 0;
    for (const provider of providers) {
      try {
        const snapshots = await this.allocationService.rebalanceProviderPool(provider.id);
        rebalanced += snapshots.length;
      } catch (err) {
        this.logger.warn(`Allocation rebalance failed for provider ${provider.id}: ${err.message}`);
      }
    }
    return { providers: providers.length, tickersRebalanced: rebalanced };
  }

  // Was AlertDigestJob (@Cron EVERY_4_HOURS). Keeps the same rolling watermark.
  async alertDigest() {
    const since = this.lastDigestAt;
    this.lastDigestAt = new Date();

    const warnings = await this.alertRepo.find({
      where: { severity: AlertSeverity.WARNING, createdAt: MoreThan(since) },
    });
    if (warnings.length === 0) return { warnings: 0 };

    const byCategory = warnings.reduce((acc, a) => {
      acc[a.category] = (acc[a.category] || 0) + 1;
      return acc;
    }, {} as Record<string, number>);
    const summary = Object.entries(byCategory).map(([category, count]) => `${category}: ${count}`).join(', ');

    await this.notificationService.createAlert(
      AlertSeverity.INFO,
      'ALERT_DIGEST',
      `${warnings.length} warning-tier alert(s) in the last digest window — ${summary}`,
    );
    return { warnings: warnings.length, summary };
  }

  // Was StrategyReevaluationJob (@Cron EVERY_DAY_AT_1AM).
  async strategyReevaluation() {
    return this.strategyEngineService.runDivergenceCheckForAllLiveStrategies();
  }

  // Was DataQualityMonitorJob (@Cron EVERY_6_HOURS).
  async dataQuality() {
    const tickers = await this.tickerRepo.find({ where: { status: TickerStatus.ACTIVE } });
    let swept = 0;
    for (const ticker of tickers) {
      try {
        await this.marketDataService.auditDataQuality(ticker.id);
        swept++;
      } catch (err) {
        this.logger.warn(`Data quality sweep failed for ticker ${ticker.id}: ${err.message}`);
      }
    }
    return { tickers: tickers.length, swept };
  }
}
