import { Injectable, NotFoundException, Inject } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Provider, ProviderType, ProviderStatus } from '../../entities/provider.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';

@Injectable()
export class ProviderService {
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
  ) {}

  async createProvider(userId: string, data: { name: string; type: ProviderType; apiKey?: string; apiSecret?: string }) {
    const provider = this.providerRepo.create({
      userId,
      name: data.name,
      type: data.type,
      status: ProviderStatus.ACTIVE,
      tradingEnabled: true,
    });
    await this.providerRepo.save(provider);

    // Save schedule defaults
    const schedule = this.scheduleRepo.create({
      providerId: provider.id,
      activeDays: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
      activeStartTime: '00:00',
      activeEndTime: '23:59',
      timezone: 'UTC',
    });
    await this.scheduleRepo.save(schedule);

    // Save risk limits defaults
    const riskLimit = this.riskLimitRepo.create({
      providerId: provider.id,
      dailyLossLimitPct: parseFloat(process.env.DEFAULT_DAILY_LOSS_LIMIT_PCT || '2.0'),
      maxConcurrentPositionsPerTicker: parseInt(process.env.DEFAULT_MAX_CONCURRENT_POSITIONS || '1', 10),
      probationSizePct: parseFloat(process.env.DEFAULT_PROBATION_SIZE_PCT || '25.0'),
    });
    await this.riskLimitRepo.save(riskLimit);

    // Save API credentials if provided
    if (data.apiKey && data.apiSecret) {
      await this.secretsProvider.storeCredential(provider.id, {
        apiKey: data.apiKey,
        apiSecret: data.apiSecret,
      });
    }

    // Initial mock/sync balance snapshot
    await this.syncBalance(provider.id, 10000); // default starting tradable balance $10,000 USDT

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
        tradableBalance: latestBalance ? latestBalance.tradableBalance : 0,
        currency: latestBalance ? latestBalance.currency : 'USDT',
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

  async syncBalance(providerId: string, customAmount?: number) {
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new NotFoundException('Provider not found');

    let balanceAmount = customAmount ?? 10000;

    // Check if real Binance API keys exist
    const creds = await this.secretsProvider.getCredential(providerId);
    if (creds && creds.apiKey && creds.apiSecret && creds.apiKey !== 'mock') {
      try {
        // In real live mode, query Binance account REST API balance
        // For demonstration & robustness fallback, if API call fails or mock key, maintain latest or fallback
      } catch (err) {
        console.warn(`Balance sync fallback for provider ${providerId}:`, err.message);
      }
    }

    const snapshot = this.balanceRepo.create({
      providerId: provider.id,
      tradableBalance: balanceAmount,
      currency: 'USDT',
    });
    return this.balanceRepo.save(snapshot);
  }

  async getLatestBalance(providerId: string) {
    const snapshot = await this.balanceRepo.findOne({
      where: { providerId },
      order: { syncedAt: 'DESC' },
    });
    return snapshot || { tradableBalance: 0, currency: 'USDT' };
  }
}
