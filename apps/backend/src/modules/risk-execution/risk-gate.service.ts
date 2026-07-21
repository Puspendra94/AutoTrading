import { Injectable, ForbiddenException, BadRequestException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { RiskLimit } from '../../entities/risk-limit.entity';
import { DailyLossTracking } from '../../entities/daily-loss-tracking.entity';
import { Position, PositionStatus, PositionSide } from '../../entities/position.entity';
import { Ticker } from '../../entities/ticker.entity';
import { ProviderBalanceSnapshot } from '../../entities/provider-balance-snapshot.entity';
import { Alert, AlertSeverity } from '../../entities/alert.entity';

export interface OrderIntent {
  tickerId: string;
  strategyId?: string;
  side: PositionSide;
  price: number;
}

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
    @InjectRepository(ProviderBalanceSnapshot)
    private readonly balanceRepo: Repository<ProviderBalanceSnapshot>,
    @InjectRepository(Alert)
    private readonly alertRepo: Repository<Alert>,
  ) {}

  /**
   * Deterministic Unbypassable Risk Gate Check (Section 5)
   */
  async evaluateOrderRiskGate(intent: OrderIntent): Promise<{ approved: boolean; allowedQuantity: number; isProbation: boolean; reason?: string }> {
    const ticker = await this.tickerRepo.findOne({ where: { id: intent.tickerId } });
    if (!ticker) throw new BadRequestException('Invalid ticker');

    const providerId = ticker.providerId;

    // 1. Fetch Risk Limits Policy for Provider
    let riskLimit = await this.riskLimitRepo.findOne({ where: { providerId } });
    if (!riskLimit) {
      riskLimit = this.riskLimitRepo.create({
        providerId,
        dailyLossLimitPct: 2.0,
        maxConcurrentPositionsPerTicker: 1,
        probationSizePct: 25.0,
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
        `Risk Gate Rejection: Daily loss limit (2.0%) breached for provider. All trading halted until reset.`,
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
          message: `CRITICAL: Daily loss limit breached for provider ${providerId}. Realized/Unrealized loss: ${currentTotalLossPct.toFixed(2)}%. Trading halted.`,
          relatedEntityType: 'Provider',
          relatedEntityId: providerId,
        }),
      );

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

    // 4. Position Sizing & Probation Multiplier (Section 5.3 & Section 2.5)
    const allocatedCapitalForTicker = cumBase * 0.2; // 20% max allocation per ticker
    const isProbation = true; // default probation sizing initially
    const sizingMultiplier = isProbation ? Number(riskLimit.probationSizePct) / 100 : 1.0;
    const capitalToUse = allocatedCapitalForTicker * sizingMultiplier;

    const allowedQuantity = Number((capitalToUse / intent.price).toFixed(4));

    return {
      approved: true,
      allowedQuantity,
      isProbation,
    };
  }
}
