import { Injectable, ForbiddenException, BadRequestException, Inject, forwardRef } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { DailyLossTracking } from '../../entities/daily-loss-tracking.entity';
import { Position, PositionStatus, PositionSide } from '../../entities/position.entity';
import { Ticker, TickerStatus } from '../../entities/ticker.entity';
import { Provider } from '../../entities/provider.entity';
import { ProviderSchedule } from '../../entities/provider-schedule.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { AllocationSnapshot } from '../../entities/allocation-snapshot.entity';
import { Strategy, StrategyStatus } from '../../entities/strategy.entity';
import { Alert, AlertSeverity } from '../../entities/alert.entity';
import { ExecutionService } from './execution.service';

export interface OrderIntent {
  tickerId: string;
  strategyId?: string;
  side: PositionSide;
  price: number;
}

const DAY_ABBREVIATIONS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

@Injectable()
export class RiskGateService {
  constructor(
    @InjectRepository(RiskLimit)
    private readonly riskLimitRepo: Repository<RiskLimit>,
    @InjectRepository(DailyLossTracking)
    private readonly dailyLossRepo: Repository<DailyLossTracking>,
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(Provider)
    private readonly providerRepo: Repository<Provider>,
    @InjectRepository(ProviderSchedule)
    private readonly scheduleRepo: Repository<ProviderSchedule>,
    @InjectRepository(ProviderBalanceSnapshot)
    private readonly balanceRepo: Repository<ProviderBalanceSnapshot>,
    @InjectRepository(AllocationSnapshot)
    private readonly allocationRepo: Repository<AllocationSnapshot>,
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
    @InjectRepository(Alert)
    private readonly alertRepo: Repository<Alert>,
    @Inject(forwardRef(() => ExecutionService))
    private readonly executionService: ExecutionService,
  ) {}

  /**
   * Deterministic Unbypassable Risk Gate Check (Section 5) — every source (Mode A
   * rules engine, Mode B live AI decision, or a manual/UI order) passes through here.
   */
  async evaluateOrderRiskGate(intent: OrderIntent): Promise<{ approved: boolean; allowedQuantity: number; isProbation: boolean; reason?: string }> {
    const ticker = await this.tickerRepo.findOne({ where: { id: intent.tickerId } });
    if (!ticker) throw new BadRequestException('Invalid ticker');

    const providerId = ticker.providerId;
    const provider = await this.providerRepo.findOne({ where: { id: providerId } });
    if (!provider) throw new BadRequestException('Invalid provider for ticker');

    // 0a. Kill switch (spec 5.4) — hard stop until manually cleared, independent of and
    // checked before the daily-loss-limit block below (which self-clears at reset).
    if (provider.killSwitchActive) {
      return {
        approved: false,
        allowedQuantity: 0,
        isProbation: false,
        reason: `Kill switch active for this provider: ${provider.killSwitchReason || 'no reason recorded'}.`,
      };
    }

    // 0b. Global/per-provider trading on/off switch (spec 4.2).
    if (!provider.tradingEnabled) {
      return {
        approved: false,
        allowedQuantity: 0,
        isProbation: false,
        reason: 'Trading is disabled for this provider.',
      };
    }

    // 0c. Provider schedule (spec 4.2) — e.g. Zerodha/Kite's exchange hours; Binance
    // defaults to 24/7 (Mon-Sun 00:00-23:59) so this is a no-op for it in practice.
    const schedule = await this.scheduleRepo.findOne({ where: { providerId } });
    if (schedule && !this.isWithinSchedule(schedule)) {
      return {
        approved: false,
        allowedQuantity: 0,
        isProbation: false,
        reason: `Outside provider's active trading schedule (${schedule.activeDays.join(',')} ${schedule.activeStartTime}-${schedule.activeEndTime} ${schedule.timezone}).`,
      };
    }

    // 1. Fetch Risk Limits Policy for Provider
    let riskLimit = await this.riskLimitRepo.findOne({ where: { providerId } });
    if (!riskLimit) {
      riskLimit = this.riskLimitRepo.create({
        providerId,
        dailyLossLimitPct: 2.0,
        maxConcurrentPositionsPerTicker: 1,
        probationSizePct: 25.0,
        probationTradesCount: 10,
      });
    }

    // 2. Daily Loss Limit Check (Section 5.1)
    const today = new Date().toISOString().split('T')[0];
    let dailyTracker = await this.dailyLossRepo.findOne({ where: { providerId, trackingDate: today } });

    const latestBalance = await this.balanceRepo.findOne({
      where: { providerId },
      order: { syncedAt: 'DESC' },
    });
    const cumBase = latestBalance ? Number(latestBalance.tradableBalance) : 10000;

    if (!dailyTracker) {
      dailyTracker = this.dailyLossRepo.create({
        providerId,
        trackingDate: today,
        capitalUnderManagementBase: cumBase,
        realizedPl: 0,
        unrealizedPl: 0,
        limitBreached: false,
      });
      await this.dailyLossRepo.save(dailyTracker);
    }

    if (dailyTracker.limitBreached) {
      throw new ForbiddenException(
        `Risk Gate Rejection: Daily loss limit (${Number(riskLimit.dailyLossLimitPct).toFixed(1)}%) breached for provider. All trading halted until reset.`,
      );
    }

    const currentTotalLossPct = Math.abs((Number(dailyTracker.realizedPl) + Number(dailyTracker.unrealizedPl)) / cumBase) * 100;
    if (currentTotalLossPct >= Number(riskLimit.dailyLossLimitPct) && (Number(dailyTracker.realizedPl) + Number(dailyTracker.unrealizedPl)) < 0) {
      dailyTracker.limitBreached = true;
      dailyTracker.breachedAt = new Date();
      await this.dailyLossRepo.save(dailyTracker);

      // Fire Critical Alert
      await this.alertRepo.save(
        this.alertRepo.create({
          severity: AlertSeverity.CRITICAL,
          category: 'RISK_GATE_KILL_SWITCH',
          message: `CRITICAL: Daily loss limit breached for provider ${providerId}. Realized/Unrealized loss: ${currentTotalLossPct.toFixed(2)}%. Trading halted, flattening all open positions.`,
          relatedEntityType: 'Provider',
          relatedEntityId: providerId,
        }),
      );

      // Spec 5.1 — "On breach: auto-flatten all open managed positions for that
      // provider," not just block new orders. Best-effort: a flatten failure must
      // never suppress the ForbiddenException that blocks the order that triggered this.
      await this.executionService
        .flattenAllPositionsForProvider(providerId, `Daily loss limit breach (${currentTotalLossPct.toFixed(2)}%)`)
        .catch(() => undefined);

      throw new ForbiddenException(`Risk Gate Triggered: Daily loss limit breached (${currentTotalLossPct.toFixed(2)}%).`);
    }

    // 3. Max Concurrent Positions Check (Section 5.2)
    const activePositionsCount = await this.positionRepo.count({
      where: { tickerId: intent.tickerId, status: PositionStatus.OPEN },
    });

    if (activePositionsCount >= Number(riskLimit.maxConcurrentPositionsPerTicker)) {
      return {
        approved: false,
        allowedQuantity: 0,
        isProbation: false,
        reason: `Max concurrent positions limit reached (${riskLimit.maxConcurrentPositionsPerTicker} max per ticker).`,
      };
    }

    // 4. Position Sizing (Section 5.3 & Section 6) — reads the Allocation Module's
    // current ceiling for this ticker rather than computing its own; the gate enforces,
    // it doesn't decide "how much should this ticker get."
    const allocatedCapitalForTicker = await this.resolveAllocationCeiling(providerId, intent.tickerId, cumBase);

    // Probation sizing (Section 2.5/12) — real trade-count check, not a permanent flag.
    // Counts every position ever opened under this strategy; once it reaches the
    // policy's probationTradesCount, sizing scales to full allocation.
    const isProbation = await this.isStrategyStillInProbation(intent.strategyId, Number(riskLimit.probationTradesCount));
    const sizingMultiplier = isProbation ? Number(riskLimit.probationSizePct) / 100 : 1.0;
    const capitalToUse = allocatedCapitalForTicker * sizingMultiplier;

    const allowedQuantity = Number((capitalToUse / intent.price).toFixed(4));

    return {
      approved: true,
      allowedQuantity,
      isProbation,
    };
  }

  /** Latest AllocationSnapshot for this ticker if a rebalance has run; falls back to an
   * even split across the provider's active-ticker count if none exists yet (e.g. the
   * very first trade before AllocationService's daily job has had a chance to run). */
  private async resolveAllocationCeiling(providerId: string, tickerId: string, cumBase: number): Promise<number> {
    const snapshot = await this.allocationRepo.findOne({
      where: { providerId, tickerId },
      order: { computedAt: 'DESC' },
    });
    if (snapshot) return Number(snapshot.allocatedCapital);

    const activeTickerCount = await this.tickerRepo.count({ where: { providerId, status: TickerStatus.ACTIVE } });
    const evenShare = activeTickerCount > 0 ? cumBase / activeTickerCount : cumBase;
    return evenShare;
  }

  /** True while the strategy has fewer than `probationTradesCount` positions opened
   * against it since going live. No strategyId (e.g. a manual/UI order) is treated as
   * probation-sized by default — the conservative side. */
  private async isStrategyStillInProbation(strategyId: string | undefined, probationTradesCount: number): Promise<boolean> {
    if (!strategyId) return true;
    const strategy = await this.strategyRepo.findOne({ where: { id: strategyId } });
    if (!strategy || strategy.status !== StrategyStatus.LIVE) return true;

    const tradesSincePromotion = await this.positionRepo.count({ where: { strategyId } });
    return tradesSincePromotion < probationTradesCount;
  }

  /** Uses Intl's built-in IANA timezone support (no extra dependency) to evaluate the
   * schedule in the provider's own configured timezone rather than assuming UTC. */
  private isWithinSchedule(schedule: ProviderSchedule): boolean {
    const now = new Date();
    let parts: Record<string, string>;
    try {
      const formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: schedule.timezone || 'UTC',
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      });
      parts = Object.fromEntries(formatter.formatToParts(now).map((p) => [p.type, p.value]));
    } catch {
      // Unrecognized timezone string — fail open rather than blocking all trading on a
      // config typo; DAY_ABBREVIATIONS/UTC fallback below.
      const day = DAY_ABBREVIATIONS[now.getUTCDay()];
      const hhmm = `${String(now.getUTCHours()).padStart(2, '0')}:${String(now.getUTCMinutes()).padStart(2, '0')}`;
      return schedule.activeDays.includes(day) && hhmm >= schedule.activeStartTime && hhmm <= schedule.activeEndTime;
    }

    const day = parts.weekday;
    const hhmm = `${parts.hour}:${parts.minute}`;
    return schedule.activeDays.includes(day) && hhmm >= schedule.activeStartTime && hhmm <= schedule.activeEndTime;
  }
}
