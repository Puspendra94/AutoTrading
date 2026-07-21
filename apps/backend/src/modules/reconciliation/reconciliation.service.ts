import { Injectable, Logger, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { ReconciliationReport, ReconciliationStatus } from '../../entities/reconciliation-report.entity';
import { Position, PositionStatus } from '../../entities/position.entity';
import { Provider, TradingMode, ProviderType } from '../../entities/provider.entity';
import { AlertSeverity } from '../../entities/alert.entity';
import { BinanceAdapter } from '../provider/adapters/binance.adapter';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { NotificationService } from '../notification/notification.service';

const MISMATCH_TOLERANCE_PCT = 5; // allows for fees/rounding noise, not a real discrepancy

@Injectable()
export class ReconciliationService {
  private readonly logger = new Logger(ReconciliationService.name);

  constructor(
    @InjectRepository(ReconciliationReport)
    private readonly reportRepo: Repository<ReconciliationReport>,
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    private readonly binanceAdapter: BinanceAdapter,
    private readonly secretsProvider: PlaintextSecretsProvider,
    private readonly notificationService: NotificationService,
  ) {}

  /**
   * Real comparison against the exchange's actual account state (spec 4.8) — distinct
   * from balance sync, which only answers "how much tradable capital is available".
   * 'paper' mode never places real orders, so there's genuinely nothing on the exchange
   * to reconcile against; that's reported as clean with an explicit note, not silently
   * skipped.
   */
  async runReconciliationAudit(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');

    const openPositions = await this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker'],
    });
    const providerPositions = openPositions.filter((p) => p.ticker?.providerId === providerId);

    const mismatches: Record<string, any>[] = [];

    if (provider.tradingMode !== TradingMode.LIVE) {
      const report = await this.saveReport(providerId, providerPositions.length, [], {
        note: `Provider is in '${provider.tradingMode}' mode — no real orders exist on the exchange to reconcile against.`,
      });
      return report;
    }

    if (provider.type !== ProviderType.BINANCE) {
      const report = await this.saveReport(providerId, providerPositions.length, [], {
        note: `Reconciliation not yet implemented for provider type '${provider.type}'.`,
      });
      return report;
    }

    const creds = await this.secretsProvider.getCredential(providerId);
    if (!creds || !creds.apiKey || !creds.apiSecret) {
      mismatches.push({ type: 'missing_credentials', detail: 'Provider is live but has no stored API credentials.' });
    } else {
      try {
        const { balances } = await this.binanceAdapter.getOpenPositionsAndBalance(
          { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
          provider.useTestnet,
        );

        const expectedByAsset = new Map<string, number>();
        for (const pos of providerPositions) {
          const baseAsset = pos.ticker.symbol.replace(/(USDT|BUSD|USDC|USD)$/, '');
          expectedByAsset.set(baseAsset, (expectedByAsset.get(baseAsset) || 0) + Number(pos.quantity));
        }

        for (const [asset, expectedQty] of expectedByAsset.entries()) {
          const real = balances.find((b) => b.asset === asset);
          const realQty = real ? real.free + real.locked : 0;
          const diffPct = expectedQty > 0 ? (Math.abs(realQty - expectedQty) / expectedQty) * 100 : 0;
          if (diffPct > MISMATCH_TOLERANCE_PCT) {
            mismatches.push({
              type: 'quantity_mismatch',
              asset,
              expectedQty: Number(expectedQty.toFixed(8)),
              exchangeQty: Number(realQty.toFixed(8)),
              divergencePct: Number(diffPct.toFixed(2)),
            });
          }
        }
      } catch (err) {
        this.logger.error(`Reconciliation exchange call failed for provider ${providerId}: ${err.message}`);
        mismatches.push({ type: 'exchange_api_error', detail: err.message });
      }
    }

    const report = await this.saveReport(providerId, providerPositions.length, mismatches);

    if (mismatches.length > 0) {
      const severity = mismatches.some((m) => m.type === 'exchange_api_error' || m.type === 'missing_credentials')
        ? AlertSeverity.CRITICAL
        : AlertSeverity.WARNING;
      await this.notificationService.createAlert(
        severity,
        'RECONCILIATION_MISMATCH',
        `Reconciliation audit found ${mismatches.length} mismatch(es) for provider ${provider.name}.`,
        'ReconciliationReport',
        report.id,
      );
    }

    return report;
  }

  private async saveReport(providerId: string, auditedPositionsCount: number, mismatches: Record<string, any>[], extra: Record<string, any> = {}) {
    const report = this.reportRepo.create({
      providerId,
      mismatchesFound: mismatches.length,
      detailJson: { auditedPositionsCount, mismatches, ...extra },
      status: mismatches.length === 0 ? ReconciliationStatus.CLEAN : ReconciliationStatus.MISMATCH,
    });
    return this.reportRepo.save(report);
  }
}
