import { Injectable, BadRequestException, Logger, Inject, forwardRef } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Position, PositionStatus, PositionSide, TradeMode, TradeNetwork } from '../../entities/position.entity';
import { Order, OrderSide, OrderType, OrderStatus } from '../../entities/order.entity';
import { Ticker } from '../../entities/ticker.entity';
import { Provider, ProviderType, TradingMode } from '../../entities/provider.entity';
import { Strategy, StrategyStatus } from '../../entities/strategy.entity';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { RiskGateService } from './risk-gate.service';
import { BinanceAdapter } from '../provider/adapters/binance.adapter';
import { PlaintextSecretsProvider } from '../../common/secrets/plaintext-secrets-provider';
import { ProviderService } from '../provider/provider.service';
import { config } from '../../config/configuration';

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
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
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

    // Stamp HOW this trade executed so the dashboard can filter by the active selection.
    // A real Binance order (isLiveOrder) is 'live' on the provider's active network; anything
    // else is a simulated paper fill, which is network-agnostic (network stays null).
    const tradeMode = isLiveOrder ? TradeMode.LIVE : TradeMode.PAPER;
    const network = isLiveOrder
      ? provider?.useTestnet
        ? TradeNetwork.TESTNET
        : TradeNetwork.MAINNET
      : null;

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
      tradeMode,
      network,
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
   * Flatten every open position for a ticker — called when a strategy is replaced so no
   * position opened by the now-retired strategy is left orphaned (the incoming strategy
   * scopes its exits to its own strategyId and would never manage it). Each position is
   * closed via closePosition, which for a live provider places the real exchange close
   * order and for paper does the simulated exit; exit price is the last mark.
   */
  async flattenOpenPositionsForTicker(tickerId: string, reason: string) {
    const openPositions = await this.positionRepo.find({
      where: { tickerId, status: PositionStatus.OPEN },
    });
    const closed: string[] = [];
    for (const position of openPositions) {
      try {
        const exitPrice = Number(position.currentPrice) || Number(position.entryPrice);
        const result = await this.closePosition(position.id, exitPrice);
        closed.push(result.position.id);
      } catch (err) {
        this.logger.error(`Strategy-switch flatten failed for position ${position.id} (${reason}): ${err.message}`);
      }
    }
    if (closed.length > 0) {
      this.logger.log(`Flattened ${closed.length} open position(s) for ticker ${tickerId} on strategy switch (${reason}).`);
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

  /**
   * The Live Trades feed: every trade (open AND closed) that matches the dashboard's currently-
   * selected Mode + Network, newest first. Paper is network-agnostic (a paper fill calls no
   * exchange), so a paper selection ignores the network filter; a live selection matches the
   * exact network the order hit. `source` mirrors trade_mode for the panel's paper/live badge.
   */
  async getTradesForView(mode: TradeMode, network: TradeNetwork | undefined, limit = 100) {
    const where: Record<string, unknown> = { tradeMode: mode };
    if (mode === TradeMode.LIVE && network) where.network = network;

    const positions = await this.positionRepo.find({
      where,
      relations: ['ticker', 'strategy'],
      order: { openedAt: 'DESC' },
      take: limit,
    });

    return positions.map((p) => ({
      ...p,
      source: p.tradeMode === TradeMode.LIVE ? 'live' : 'paper',
    }));
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

  /**
   * Manual paper-only test trade (dashboard "Test paper trade" button): opens a long on the
   * ticker's active strategy at `price` when flat, or closes the open one — so paper P/L, ROI and
   * the trade feed can be exercised on demand without waiting for a live signal. Refuses in LIVE
   * mode so it can never place a real exchange order by accident.
   */
  async placeTestPaperTrade(tickerId: string, price: number) {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    if (!ticker) throw new BadRequestException('Invalid ticker');
    const provider = await this.providerRepo.findOne({ where: { id: ticker.providerId } });
    if (provider?.tradingMode === TradingMode.LIVE) {
      throw new BadRequestException('Test trade is paper-only — switch the provider to PAPER mode first.');
    }
    if (!price || price <= 0) throw new BadRequestException('A positive price is required.');

    const strategy = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
    const strategyId = strategy?.id;

    const openPosition = await this.positionRepo.findOne({
      where: { tickerId, status: PositionStatus.OPEN },
    });
    if (openPosition) {
      const { position } = await this.closePosition(openPosition.id, price);
      return { action: 'closed', position };
    }
    const result = await this.executeTradeSignal(tickerId, PositionSide.LONG, price, strategyId);
    return { action: 'opened', ...result };
  }

  /**
   * Paper-trading performance summary for the dashboard: a simulated equity curve seeded by a
   * fixed starting balance (config.paper.startingBalanceUsd) plus realized P/L from closed paper
   * trades and unrealized P/L from open ones. Paper is network-agnostic (simulated fills touch no
   * exchange), so this aggregates ALL paper positions regardless of testnet/mainnet.
   */
  async getPaperSummary() {
    const positions = await this.positionRepo.find({ where: { tradeMode: TradeMode.PAPER } });
    const open = positions.filter((p) => p.status === PositionStatus.OPEN);
    const closed = positions.filter((p) => p.status === PositionStatus.CLOSED);

    const realizedPl = closed.reduce((s, p) => s + Number(p.realizedPl || 0), 0);
    const unrealizedPl = open.reduce((s, p) => s + Number(p.unrealizedPl || 0), 0);
    const investedInOpen = open.reduce((s, p) => s + Number(p.entryPrice) * Number(p.quantity), 0);
    const starting = config.paper.startingBalanceUsd;
    const equity = starting + realizedPl + unrealizedPl;
    const availableBalance = starting + realizedPl - investedInOpen;
    const roiPct = starting > 0 ? ((equity - starting) / starting) * 100 : 0;
    const wins = closed.filter((p) => Number(p.realizedPl) > 0).length;
    const winRate = closed.length > 0 ? (wins / closed.length) * 100 : 0;

    const r2 = (n: number) => Number(n.toFixed(2));
    return {
      startingBalance: r2(starting),
      equity: r2(equity),
      availableBalance: r2(availableBalance),
      realizedPl: r2(realizedPl),
      unrealizedPl: r2(unrealizedPl),
      roiPct: r2(roiPct),
      openCount: open.length,
      closedCount: closed.length,
      totalTrades: positions.length,
      winRate: r2(winRate),
    };
  }
}
