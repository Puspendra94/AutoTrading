import { Injectable, BadRequestException, Logger, Inject, forwardRef } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Position, PositionStatus, PositionSide } from '../../entities/position.entity';
import { Order, OrderSide, OrderType, OrderStatus } from '../../entities/order.entity';
import { Ticker } from '../../entities/ticker.entity';
import { Provider, ProviderType, TradingMode } from '../../entities/provider.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { RiskGateService } from './risk-gate.service';
import { BinanceAdapter } from '../provider/adapters/binance.adapter';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { ProviderService } from '../provider/provider.service';

@Injectable()
export class ExecutionService {
  private readonly logger = new Logger(ExecutionService.name);

  constructor(
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    @InjectRepository(Order)
    private readonly orderRepo: Repository<Order>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    @InjectRepository(RiskLimit)
    private readonly riskLimitRepo: Repository<RiskLimit>,
    @Inject(forwardRef(() => RiskGateService))
    private readonly riskGateService: RiskGateService,
    private readonly binanceAdapter: BinanceAdapter,
    private readonly secretsProvider: PlaintextSecretsProvider,
    @Inject(forwardRef(() => ProviderService))
    private readonly providerService: ProviderService,
  ) {}

  async executeTradeSignal(tickerId: string, side: PositionSide, price: number, strategyId?: string) {
    // 1. Pass through the risk gate — unconditional for every source (Mode A rules,
    // Mode B live AI decision, or a manual/UI-triggered order). Never bypassed.
    const riskCheck = await this.riskGateService.evaluateOrderRiskGate({
      tickerId,
      strategyId,
      side,
      price,
    });

    if (!riskCheck.approved) {
      return { status: 'REJECTED', reason: riskCheck.reason };
    }

    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    if (!ticker) throw new BadRequestException('Invalid ticker');
    const provider = await this.providerRepo.findOne({ where: { id: ticker.providerId } });

    let fillPrice = price;
    let fillQuantity = riskCheck.allowedQuantity;
    let providerOrderId = `SIM_${Date.now()}`;
    let isLiveOrder = false;

    // 2. Only ever place a real order when the provider is explicitly in 'live' mode
    // AND real credentials exist — this is the spec 0.2 checkpoint's concrete
    // enforcement point. Otherwise, fall through to the simulated local fill, seeded
    // by the real streamed price already passed in by the caller.
    if (provider && provider.tradingMode === TradingMode.LIVE && provider.type === ProviderType.BINANCE) {
      const creds = await this.secretsProvider.getCredential(provider.id, provider.useTestnet);
      if (!creds || !creds.apiKey || !creds.apiSecret) {
        throw new BadRequestException(
          `Provider ${provider.name} is set to 'live' trading mode but has no stored API credentials — cannot place a real order.`,
        );
      }
      try {
        const orderSide = side === PositionSide.LONG ? 'BUY' : 'SELL';
        const result = await this.binanceAdapter.placeMarketOrder(
          { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
          provider.useTestnet,
          ticker.symbol,
          orderSide,
          riskCheck.allowedQuantity,
        );
        fillPrice = result.fillPrice || price;
        fillQuantity = result.fillQuantity || riskCheck.allowedQuantity;
        providerOrderId = result.orderId;
        isLiveOrder = true;
        await this.providerService.recordApiSuccess(provider.id);
      } catch (err) {
        this.logger.error(`Live order placement failed on Binance for ${ticker.symbol}: ${err.message}`);
        await this.providerService.recordApiFailure(provider.id);
        return { status: 'REJECTED', reason: `Exchange order placement failed: ${err.message}` };
      }
    }

    const position = this.positionRepo.create({
      tickerId,
      strategyId,
      side,
      status: PositionStatus.OPEN,
      entryPrice: fillPrice,
      currentPrice: fillPrice,
      quantity: fillQuantity,
      unrealizedPl: 0,
      realizedPl: 0,
      isProbation: riskCheck.isProbation,
    });
    await this.positionRepo.save(position);

    const order = this.orderRepo.create({
      positionId: position.id,
      providerOrderId,
      side: side === PositionSide.LONG ? OrderSide.BUY : OrderSide.SELL,
      quantity: fillQuantity,
      price: fillPrice,
      orderType: OrderType.MARKET,
      status: OrderStatus.FILLED,
      filledAt: new Date(),
    });
    await this.orderRepo.save(order);

    return { status: 'EXECUTED', position, order, isLiveOrder };
  }

  async closePosition(positionId: string, exitPrice: number) {
    const position = await this.positionRepo.findOne({ where: { id: positionId }, relations: ['ticker'] });
    if (!position || position.status === PositionStatus.CLOSED) {
      throw new BadRequestException('Position not found or already closed');
    }

    let finalExitPrice = exitPrice;
    let providerOrderId = `SIM_exit_${Date.now()}`;

    const ticker = position.ticker;
    const provider = ticker ? await this.providerRepo.findOne({ where: { id: ticker.providerId } }) : null;

    if (provider && provider.tradingMode === TradingMode.LIVE && provider.type === ProviderType.BINANCE) {
      const creds = await this.secretsProvider.getCredential(provider.id, provider.useTestnet);
      if (creds?.apiKey && creds?.apiSecret) {
        try {
          const closeSide = position.side === PositionSide.LONG ? 'SELL' : 'BUY';
          const result = await this.binanceAdapter.placeMarketOrder(
            { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
            provider.useTestnet,
            ticker.symbol,
            closeSide,
            Number(position.quantity),
          );
          finalExitPrice = result.fillPrice || exitPrice;
          providerOrderId = result.orderId;
        } catch (err) {
          this.logger.error(`Live close-order failed for position ${positionId}: ${err.message}`);
          throw new BadRequestException(`Exchange close-order failed: ${err.message}`);
        }
      }
    }

    const priceDiff = finalExitPrice - Number(position.entryPrice);
    const realizedPl = Number(
      (position.side === PositionSide.LONG ? priceDiff : -priceDiff) * Number(position.quantity),
    );

    position.status = PositionStatus.CLOSED;
    position.exitPrice = finalExitPrice;
    position.currentPrice = finalExitPrice;
    position.realizedPl = Number(realizedPl.toFixed(8));
    position.unrealizedPl = 0;
    position.closedAt = new Date();
    await this.positionRepo.save(position);

    const closeOrder = this.orderRepo.create({
      positionId: position.id,
      providerOrderId,
      side: position.side === PositionSide.LONG ? OrderSide.SELL : OrderSide.BUY,
      quantity: Number(position.quantity),
      price: finalExitPrice,
      orderType: OrderType.MARKET,
      status: OrderStatus.FILLED,
      filledAt: new Date(),
    });
    await this.orderRepo.save(closeOrder);

    return { position, closeOrder };
  }

  /**
   * Spec 5.1 — "On breach: auto-flatten all open managed positions for that provider."
   * Called by RiskGateService the moment a daily-loss breach is detected, not on a
   * delay/schedule. Exits each open position at its last-known mark price (updated
   * live by MarketStreamService) rather than blocking on a fresh price fetch, since
   * getting out is more urgent than getting the exact last tick.
   */
  async flattenAllPositionsForProvider(providerId: string, reason: string) {
    const openPositions = await this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker'],
    });
    const providerPositions = openPositions.filter((p) => p.ticker?.providerId === providerId);

    const closed = [];
    for (const position of providerPositions) {
      try {
        const exitPrice = Number(position.currentPrice) || Number(position.entryPrice);
        const result = await this.closePosition(position.id, exitPrice);
        closed.push(result.position.id);
      } catch (err) {
        this.logger.error(`Auto-flatten failed for position ${position.id} (${reason}): ${err.message}`);
      }
    }
    return { flattenedCount: closed.length, positionIds: closed };
  }

  /**
   * Hard exit guardrail (Profile-configurable): closes any open position whose P/L breaches
   * the provider's hard stop-loss / take-profit cap, independent of the strategy's own exit
   * logic. Called on every live tick by MarketStreamService, so a runaway loss is cut even
   * intra-candle. Null caps mean "no hard limit — defer to the strategy".
   */
  async enforceHardExits(tickerId: string, lastPrice: number) {
    const positions = await this.positionRepo.find({
      where: { tickerId, status: PositionStatus.OPEN },
      relations: ['ticker'],
    });
    if (positions.length === 0) return;

    const limitByProvider = new Map<string, RiskLimit | null>();
    for (const pos of positions) {
      const providerId = pos.ticker?.providerId;
      if (!providerId) continue;
      if (!limitByProvider.has(providerId)) {
        limitByProvider.set(providerId, await this.riskLimitRepo.findOne({ where: { providerId } }));
      }
      const limit = limitByProvider.get(providerId);
      if (!limit) continue;

      const sl = limit.hardStopLossPct != null ? Number(limit.hardStopLossPct) : null;
      const tp = limit.hardTakeProfitPct != null ? Number(limit.hardTakeProfitPct) : null;
      if (sl == null && tp == null) continue;

      const entry = Number(pos.entryPrice);
      if (!entry) continue;
      const pnlPct =
        pos.side === PositionSide.LONG
          ? ((lastPrice - entry) / entry) * 100
          : ((entry - lastPrice) / entry) * 100;

      if (sl != null && pnlPct <= -sl) {
        this.logger.warn(`Hard stop-loss hit for position ${pos.id} (${pnlPct.toFixed(2)}% ≤ -${sl}%). Closing.`);
        await this.closePosition(pos.id, lastPrice).catch((e) =>
          this.logger.error(`Hard stop-loss close failed for ${pos.id}: ${e.message}`),
        );
      } else if (tp != null && pnlPct >= tp) {
        this.logger.warn(`Hard take-profit hit for position ${pos.id} (${pnlPct.toFixed(2)}% ≥ ${tp}%). Closing.`);
        await this.closePosition(pos.id, lastPrice).catch((e) =>
          this.logger.error(`Hard take-profit close failed for ${pos.id}: ${e.message}`),
        );
      }
    }
  }

  async getOpenPositions() {
    return this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker', 'strategy'],
      order: { openedAt: 'DESC' },
    });
  }

  /**
   * Open positions annotated against the real exchange so the dashboard reflects Binance
   * reality (spec: no ghost trades). `source` is 'paper' (simulated fill, SIM_* order — never
   * on Binance) or 'live'. For live positions on a connected Binance provider, `exchangeVerified`
   * is true when the account actually holds the position's base asset, false when it doesn't
   * (a stale/ghost trade to flag), or null when it can't be checked (no creds / API error).
   */
  async getOpenPositionsReconciled() {
    const positions = await this.positionRepo.find({
      where: { status: PositionStatus.OPEN },
      relations: ['ticker', 'strategy', 'orders'],
      order: { openedAt: 'DESC' },
    });

    const providerCache = new Map<string, Provider | null>();
    const accountCache = new Map<string, { asset: string; free: number; locked: number }[] | null>();

    const results = [];
    for (const p of positions) {
      const providerId = p.ticker?.providerId;
      const isSim = (p.orders || []).some((o) => (o.providerOrderId || '').startsWith('SIM_'));
      const source: 'paper' | 'live' = isSim ? 'paper' : 'live';
      let exchangeVerified: boolean | null = null;

      if (source === 'live' && providerId) {
        if (!providerCache.has(providerId)) {
          providerCache.set(providerId, await this.providerRepo.findOne({ where: { id: providerId } }));
        }
        const provider = providerCache.get(providerId);
        if (provider && provider.tradingMode === TradingMode.LIVE && provider.type === ProviderType.BINANCE) {
          if (!accountCache.has(providerId)) {
            let balances: { asset: string; free: number; locked: number }[] | null = null;
            try {
              const creds = await this.secretsProvider.getCredential(providerId, provider.useTestnet);
              if (creds?.apiKey && creds?.apiSecret) {
                const account = await this.binanceAdapter.getOpenPositionsAndBalance(
                  { apiKey: creds.apiKey, apiSecret: creds.apiSecret },
                  provider.useTestnet,
                );
                balances = account.balances;
              }
            } catch (err) {
              this.logger.warn(`Position reconciliation: account fetch failed for provider ${providerId}: ${err.message}`);
            }
            accountCache.set(providerId, balances);
          }
          const balances = accountCache.get(providerId);
          if (balances && p.side === PositionSide.LONG) {
            const base = this.baseAsset(p.ticker.symbol);
            const bal = balances.find((b) => b.asset === base);
            const held = bal ? bal.free + bal.locked : 0;
            // Real fills lose a little to fees, so require ~90% of the position size on hand.
            exchangeVerified = held >= Number(p.quantity) * 0.9;
          }
        }
      }

      const { orders, ...rest } = p; // orders were only needed to detect SIM_* (paper) fills
      results.push({ ...rest, source, exchangeVerified });
    }
    return results;
  }

  /** Base asset from a spot symbol (BTCUSDT -> BTC), stripping the common quote assets. */
  private baseAsset(symbol: string): string {
    for (const quote of ['USDT', 'USDC', 'BUSD', 'FDUSD', 'TUSD', 'USD']) {
      if (symbol.endsWith(quote)) return symbol.slice(0, -quote.length);
    }
    return symbol;
  }

  async getAllPositions() {
    return this.positionRepo.find({
      relations: ['ticker', 'strategy'],
      order: { openedAt: 'DESC' },
      take: 100,
    });
  }
}
