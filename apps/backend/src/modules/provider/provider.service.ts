import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Provider, ProviderType, ProviderStatus, TradingMode } from '../../entities/provider.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { BinanceAdapter } from './adapters/binance.adapter';
import { NotificationService } from '../notification/notification.service';
import { AlertSeverity } from '../../entities/alert.entity';

const API_FAILURE_KILL_THRESHOLD = parseInt(process.env.PROVIDER_API_FAILURE_THRESHOLD || '5', 10);

@Injectable()
export class ProviderService {
  private readonly logger = new Logger(ProviderService.name);

  constructor(
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    @InjectRepository(ProviderBalanceSnapshot)
    private readonly balanceRepo: Repository<ProviderBalanceSnapshot>,
    @InjectRepository(ProviderSchedule)
    private readonly scheduleRepo: Repository<ProviderSchedule>,
    @InjectRepository(RiskLimit)
    private readonly riskLimitRepo: Repository<RiskLimit>,
    private readonly secretsProvider: PlaintextSecretsProvider,
    private readonly binanceAdapter: BinanceAdapter,
    private readonly notificationService: NotificationService,
  ) {}

  async createProvider(
    userId: string,
    data: {
      name: string;
      type: ProviderType;
      apiKey?: string;
      apiSecret?: string;
      tradingMode?: TradingMode;
      useTestnet?: boolean;
    },
  ) {
    const provider = this.providerRepo.create({
      userId,
      name: data.name,
      type: data.type,
      status: ProviderStatus.ACTIVE,
      tradingEnabled: true,
      tradingMode: data.tradingMode || TradingMode.PAPER,
      useTestnet: data.useTestnet !== undefined ? data.useTestnet : true,
    });
    await this.providerRepo.save(provider);

    const schedule = this.scheduleRepo.create({
      providerId: provider.id,
      activeDays: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
      activeStartTime: '00:00',
      activeEndTime: '23:59',
      timezone: 'UTC',
    });
    await this.scheduleRepo.save(schedule);

    const riskLimit = this.riskLimitRepo.create({
      providerId: provider.id,
      dailyLossLimitPct: parseFloat(process.env.DEFAULT_DAILY_LOSS_LIMIT_PCT || '2.0'),
      maxConcurrentPositionsPerTicker: parseInt(process.env.DEFAULT_MAX_CONCURRENT_POSITIONS || '1', 10),
      probationSizePct: parseFloat(process.env.DEFAULT_PROBATION_SIZE_PCT || '25.0'),
      probationTradesCount: parseInt(process.env.DEFAULT_PROBATION_TRADES_COUNT || '10', 10),
    });
    await this.riskLimitRepo.save(riskLimit);

    if (data.apiKey && data.apiSecret) {
      await this.secretsProvider.storeCredential(
        provider.id,
        { apiKey: data.apiKey, apiSecret: data.apiSecret },
        provider.useTestnet,
      );
      // Best-effort immediate sync so the UI doesn't sit on a stale "not synced" state
      // longer than necessary; failure here doesn't block provider creation.
      await this.syncBalance(provider.id).catch((err) =>
        this.logger.warn(`Initial balance sync failed for provider ${provider.id}: ${err.message}`),
      );
    }

    return provider;
  }

  async listProviders(userId: string) {
    const providers = await this.providerRepo.find({ where: { userId } });
    const results = [];
    for (const p of providers) {
      const latestBalance = await this.balanceRepo.findOne({
        where: { providerId: p.id },
        order: { syncedAt: 'DESC' },
      });
      const networks = await this.secretsProvider.getConfiguredNetworks(p.id);
      const activeNetworkHasKeys = p.useTestnet ? networks.testnet : networks.mainnet;
      results.push({
        ...p,
        hasCredentials: activeNetworkHasKeys, // keys for the CURRENTLY-selected network
        networks, // which networks have keys — drives the header toggle's "add keys" prompt
        tradableBalance: latestBalance ? Number(latestBalance.tradableBalance) : null,
        currency: latestBalance ? latestBalance.currency : null,
        lastSyncedAt: latestBalance ? latestBalance.syncedAt : null,
      });
    }
    return results;
  }

  async toggleTrading(providerId: string, enabled: boolean) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    provider.tradingEnabled = enabled;
    return this.providerRepo.save(provider);
  }

  /**
   * Real kill switch (spec 5.4) — a hard stop on all new orders for this provider
   * until manually cleared, distinct from the daily-loss-limit block (which clears
   * itself at the next reset boundary). Triggered manually here, or automatically by
   * ReconciliationService (serious mismatch) / recordApiFailure (repeated failures).
   */
  async triggerKillSwitch(providerId: string, reason: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    if (provider.killSwitchActive) return provider;

    provider.killSwitchActive = true;
    provider.killSwitchReason = reason;
    await this.providerRepo.save(provider);

    await this.notificationService.createAlert(
      AlertSeverity.CRITICAL,
      'KILL_SWITCH_TRIGGERED',
      `Kill switch triggered for provider ${provider.name}: ${reason}. All new orders blocked until manually cleared.`,
      'Provider',
      provider.id,
    );

    return provider;
  }

  async clearKillSwitch(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    provider.killSwitchActive = false;
    provider.killSwitchReason = null;
    provider.apiFailureCount = 0;
    return this.providerRepo.save(provider);
  }

  /** Repeated provider API failures auto-trip the kill switch (spec 5.4) rather than
   * silently retrying forever — call from any provider-API call site's catch block. */
  async recordApiFailure(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider || provider.killSwitchActive) return;

    provider.apiFailureCount += 1;
    if (provider.apiFailureCount >= API_FAILURE_KILL_THRESHOLD) {
      await this.providerRepo.save(provider);
      await this.triggerKillSwitch(
        providerId,
        `${provider.apiFailureCount} consecutive provider API failures (threshold ${API_FAILURE_KILL_THRESHOLD}).`,
      );
    } else {
      await this.providerRepo.save(provider);
    }
  }

  /** Call from any provider-API call site's success path to reset the failure streak. */
  async recordApiSuccess(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (provider && provider.apiFailureCount !== 0) {
      provider.apiFailureCount = 0;
      await this.providerRepo.save(provider);
    }
  }

  async updateTradingMode(providerId: string, tradingMode?: TradingMode, useTestnet?: boolean) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    if (tradingMode !== undefined) provider.tradingMode = tradingMode;
    if (useTestnet !== undefined) provider.useTestnet = useTestnet;
    return this.providerRepo.save(provider);
  }

  /**
   * Real authenticated balance sync — no fake/default seeding. If no credentials are
   * stored yet, or the exchange call fails, this throws rather than silently returning
   * a placeholder number; callers (controller, worker cron) decide how to surface that.
   */
  async syncBalance(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');

    const creds = await this.secretsProvider.getCredential(providerId, provider.useTestnet);
    if (!creds || !creds.apiKey || !creds.apiSecret) {
      throw new BadRequestException(
        `No API credentials stored for the ${provider.useTestnet ? 'testnet' : 'mainnet'} network — connect Binance API key/secret for this network before syncing balance.`,
      );
    }

    if (provider.type !== ProviderType.BINANCE) {
      throw new BadRequestException(`Balance sync not yet implemented for provider type '${provider.type}'.`);
    }

    let balances;
    try {
      balances = await this.binanceAdapter.getAccountBalance(
        { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
        provider.useTestnet,
      );
      await this.recordApiSuccess(providerId);
    } catch (err) {
      await this.recordApiFailure(providerId);
      throw err;
    }

    // "Tradable balance" (spec 2.1) = free USDT (or the primary quote asset). Fall back
    // to summing all free balances only if USDT isn't present, so a non-empty testnet
    // wallet in other assets still shows something meaningful.
    const usdt = balances.find((b) => b.asset === 'USDT');
    const tradableBalance = usdt ? usdt.free : balances.reduce((sum, b) => sum + b.free, 0);
    const currency = usdt ? 'USDT' : balances[0]?.asset || 'USDT';

    const snapshot = this.balanceRepo.create({
      providerId: provider.id,
      tradableBalance,
      currency,
    });
    return this.balanceRepo.save(snapshot);
  }

  /**
   * Live account snapshot straight from Binance — real balances (free/locked per asset)
   * plus real open orders. Powers the connected Connector view's balance cards and the
   * Active Orders table. Fetched on demand so the UI reflects the account in real time.
   */
  async getLiveAccount(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');

    const creds = await this.secretsProvider.getCredential(providerId, provider.useTestnet);
    if (!creds || !creds.apiKey || !creds.apiSecret) {
      throw new BadRequestException(
        `No API credentials stored for the ${provider.useTestnet ? 'testnet' : 'mainnet'} network.`,
      );
    }
    if (provider.type !== ProviderType.BINANCE) {
      throw new BadRequestException(`Live account not supported for provider type '${provider.type}'.`);
    }

    let account;
    try {
      account = await this.binanceAdapter.getOpenPositionsAndBalance(
        { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
        provider.useTestnet,
      );
      await this.recordApiSuccess(providerId);
    } catch (err) {
      await this.recordApiFailure(providerId);
      throw err;
    }

    const usdt = account.balances.find((b) => b.asset === 'USDT');
    return {
      balances: account.balances,
      openOrders: account.openOrders,
      totals: {
        // Denominated in USDT (the product's quote asset) from real free/locked values.
        available: usdt ? usdt.free : 0,
        inOrders: usdt ? usdt.locked : 0,
        total: usdt ? usdt.free + usdt.locked : 0,
        currency: 'USDT',
      },
    };
  }

  /**
   * Disconnect a provider — deletes its stored API credentials (so it drops back to the
   * "not connected" state / paper mode) without deleting the provider row, its risk limits,
   * or its balance history.
   */
  async disconnect(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    await this.secretsProvider.deleteCredential(providerId);
    provider.tradingMode = TradingMode.PAPER; // can't trade live without credentials
    await this.providerRepo.save(provider);
    return { disconnected: true };
  }

  /** Add/replace the API keys for one network on an existing provider (the header toggle's
   * "add {network} keys" flow). Does not create a new provider; the other network's keys are
   * preserved. Syncs the balance when the added network is the one currently selected. */
  async addCredential(providerId: string, apiKey: string, apiSecret: string, useTestnet: boolean) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    if (!apiKey || !apiSecret) throw new BadRequestException('apiKey and apiSecret are required.');

    await this.secretsProvider.storeCredential(providerId, { apiKey, apiSecret }, useTestnet);

    if (provider.useTestnet === useTestnet) {
      await this.syncBalance(providerId).catch((err) =>
        this.logger.warn(`Balance sync after adding ${useTestnet ? 'testnet' : 'mainnet'} keys failed: ${err.message}`),
      );
    }
    return { added: true, network: useTestnet ? 'testnet' : 'mainnet' };
  }

  /** Risk-limit guardrails for a provider (created lazily with defaults if none exist),
   * surfaced to and edited from the Profile page. */
  async getRiskLimit(providerId: string) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    let riskLimit = await this.riskLimitRepo.findOne({ where: { providerId } });
    if (!riskLimit) {
      riskLimit = this.riskLimitRepo.create({ providerId });
      await this.riskLimitRepo.save(riskLimit);
    }
    return riskLimit;
  }

  async updateRiskLimit(
    providerId: string,
    patch: Partial<Pick<RiskLimit,
      'dailyLossLimitPct' | 'maxConcurrentPositionsPerTicker' | 'probationSizePct' |
      'probationTradesCount' | 'resetBoundary' | 'hardStopLossPct' | 'hardTakeProfitPct'>>,
  ) {
    const riskLimit = await this.getRiskLimit(providerId);
    // Only the hard-cap fields are nullable ("blank = no cap"); for the NOT-NULL fields a
    // null/blank is ignored so a partial save can't wipe a required guardrail.
    const nullable = new Set(['hardStopLossPct', 'hardTakeProfitPct']);
    for (const key of [
      'dailyLossLimitPct', 'maxConcurrentPositionsPerTicker', 'probationSizePct',
      'probationTradesCount', 'resetBoundary', 'hardStopLossPct', 'hardTakeProfitPct',
    ] as const) {
      const val = patch[key];
      if (val === undefined) continue;
      if (val === null && !nullable.has(key)) continue;
      (riskLimit as any)[key] = val;
    }
    return this.riskLimitRepo.save(riskLimit);
  }

  async getLatestBalance(providerId: string) {
    const snapshot = await this.balanceRepo.findOne({
      where: { providerId },
      order: { syncedAt: 'DESC' },
    });
    if (!snapshot) {
      return { tradableBalance: null, currency: null, synced: false };
    }
    return { tradableBalance: Number(snapshot.tradableBalance), currency: snapshot.currency, synced: true, syncedAt: snapshot.syncedAt };
  }

  async getProviderEntity(providerId: string): Promise<Provider> {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');
    return provider;
  }

  async listProvidersWithCredentials(): Promise<Provider[]> {
    // Used by the worker's scheduled balance-sync job — only providers that have ever
    // had credentials stored are worth attempting (avoids noisy failed syncs for
    // providers the user hasn't connected real keys to yet).
    const providers = await this.providerRepo.find();
    const withCreds: Provider[] = [];
    for (const p of providers) {
      const creds = await this.secretsProvider.getCredential(p.id);
      if (creds && creds.apiKey && creds.apiSecret) withCreds.push(p);
    }
    return withCreds;
  }
}
