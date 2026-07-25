import { Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import * as bcrypt from 'bcryptjs';
import * as crypto from 'crypto';
import { User } from '../../entities/user.entity';
import { Provider, ProviderType, ProviderStatus, TradingMode } from '../../entities/provider.entity';
import { MarketType } from '../../entities/market-type.entity';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { MarketDataService } from './market-data.service';

const SYSTEM_EMAIL = 'system@autotrade.local';
const SYSTEM_PROVIDER_NAME = 'Binance Public Data';
export const MARKET_DATA_SYMBOL = 'BTCUSDT';
export const MARKET_DATA_INTERVAL = '1m';

/**
 * Ensures market data (chart + live price) works with zero connector setup. Binance's
 * klines REST + kline WebSocket are public (no API key), so we seed a credential-less
 * "system" provider that owns the canonical BTCUSDT 1m ticker. The Connector page is then
 * only about *trading* (real API keys, balances, positions) — never a prerequisite for
 * seeing the market. Runs once on API startup.
 */
@Injectable()
export class MarketDataSeedService {
  private readonly logger = new Logger(MarketDataSeedService.name);

  constructor(
    @InjectRepository(User) private readonly userRepo: Repository<User>,
    @InjectRepository(Provider) private readonly providerRepo: Repository<Provider>,
    @InjectRepository(MarketType) private readonly marketTypeRepo: Repository<MarketType>,
    @InjectRepository(Ticker) private readonly tickerRepo: Repository<Ticker>,
    private readonly marketDataService: MarketDataService,
  ) {}

  async ensureSystemMarketDataTicker(): Promise<Ticker> {
    // Reuse any existing BTCUSDT 1m ticker if one is already present (e.g. added earlier);
    // otherwise create it under the system provider. Either way market data has a home.
    let ticker = await this.tickerRepo.findOne({
      where: { symbol: MARKET_DATA_SYMBOL, interval: MARKET_DATA_INTERVAL },
      order: { createdAt: 'ASC' },
    });

    if (!ticker) {
      const provider = await this.ensureSystemProvider();
      const marketType = await this.ensureSpotMarketType(provider.id);
      ticker = this.tickerRepo.create({
        providerId: provider.id,
        marketTypeId: marketType.id,
        symbol: MARKET_DATA_SYMBOL,
        interval: MARKET_DATA_INTERVAL,
        status: TickerStatus.ACTIVE,
        onboardingStage: OnboardingStage.READY,
      });
      await this.tickerRepo.save(ticker);
      this.logger.log(`Seeded system market-data ticker ${MARKET_DATA_SYMBOL}@${MARKET_DATA_INTERVAL} (${ticker.id}).`);
    } else if (ticker.status === TickerStatus.INACTIVE) {
      ticker.status = TickerStatus.ACTIVE;
      await this.tickerRepo.save(ticker);
    }

    // Give the chart something to show immediately from public klines (no credentials).
    // The Python worker fills full history separately; this is just the first 1000 candles.
    const existing = await this.marketDataService.getCandles(ticker.id, 1);
    if (existing.length === 0) {
      this.marketDataService
        .backfillHistoricalData(ticker.id)
        .catch((err) => this.logger.warn(`Initial public backfill failed for ${ticker.id}: ${err.message}`));
    }

    return ticker;
  }

  private async ensureSystemProvider(): Promise<Provider> {
    let user = await this.userRepo.findOne({ where: { email: SYSTEM_EMAIL } });
    if (!user) {
      // Random, un-loginable password — this account exists only to own the public
      // market-data provider (providers require a user FK).
      const passwordHash = await bcrypt.hash(crypto.randomBytes(24).toString('hex'), 10);
      user = this.userRepo.create({ email: SYSTEM_EMAIL, passwordHash });
      await this.userRepo.save(user);
    }

    let provider = await this.providerRepo.findOne({ where: { userId: user.id, name: SYSTEM_PROVIDER_NAME } });
    if (!provider) {
      provider = this.providerRepo.create({
        userId: user.id,
        name: SYSTEM_PROVIDER_NAME,
        type: ProviderType.BINANCE,
        status: ProviderStatus.ACTIVE,
        tradingEnabled: false, // public data only — never trades
        tradingMode: TradingMode.PAPER,
        useTestnet: true,
      });
      await this.providerRepo.save(provider);
    }
    return provider;
  }

  private async ensureSpotMarketType(providerId: string): Promise<MarketType> {
    let marketType = await this.marketTypeRepo.findOne({ where: { providerId, name: 'spot' } });
    if (!marketType) {
      marketType = this.marketTypeRepo.create({ providerId, name: 'spot' });
      await this.marketTypeRepo.save(marketType);
    }
    return marketType;
  }
}
