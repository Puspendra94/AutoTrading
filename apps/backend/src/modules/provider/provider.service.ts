import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Provider, ProviderType, ProviderStatus, TradingMode } from '../../entities/provider.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { BinanceAdapter } from './adapters/binance.adapter';

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
    });
    await this.riskLimitRepo.save(riskLimit);

    if (data.apiKey && data.apiSecret) {
      await this.secretsProvider.storeCredential(provider.id, {
        apiKey: data.apiKey,
        apiSecret: data.apiSecret,
      });
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
      const creds = await this.secretsProvider.getCredential(p.id);
      results.push({
        ...p,
        hasCredentials: !!creds,
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

    const creds = await this.secretsProvider.getCredential(providerId);
    if (!creds || !creds.apiKey || !creds.apiSecret) {
      throw new BadRequestException(
        'No API credentials stored for this provider yet — connect real Binance API key/secret before syncing balance.',
      );
    }

    if (provider.type !== ProviderType.BINANCE) {
      throw new BadRequestException(`Balance sync not yet implemented for provider type '${provider.type}'.`);
    }

    const balances = await this.binanceAdapter.getAccountBalance(
      { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
      provider.useTestnet,
    );

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
