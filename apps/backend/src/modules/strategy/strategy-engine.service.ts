import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { In, Repository } from 'typeorm';
import { Strategy, StrategyStatus } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { ExecutionService } from '../risk-execution/execution.service';

/**
 * Strategy read/promote surface for the API (consolidation Phase B). Generation, backtesting, the
 * DSL interpreter, live-signal evaluation, divergence checks and lesson recording all moved to the
 * Python worker — this service now only serves the frontend's read endpoints and the operator's
 * manual "activate this version" override.
 */
@Injectable()
export class StrategyEngineService {
  private readonly logger = new Logger(StrategyEngineService.name);

  constructor(
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
    @InjectRepository(BacktestResult)
    private readonly backtestRepo: Repository<BacktestResult>,
    @InjectRepository(StrategyEvaluationPolicy)
    private readonly policyRepo: Repository<StrategyEvaluationPolicy>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    private readonly executionService: ExecutionService,
  ) {}

  async getActiveStrategyForTicker(tickerId: string) {
    const strategy = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
    if (!strategy) return null;
    const backtest = await this.backtestRepo.findOne({ where: { strategyId: strategy.id } });
    return { strategy, backtest };
  }

  async getStrategiesByTicker(tickerId: string) {
    const strategies = await this.strategyRepo.find({ where: { tickerId }, order: { version: 'DESC' } });
    if (!strategies.length) return [];
    const backtests = await this.backtestRepo.find({ where: { strategyId: In(strategies.map((s) => s.id)) } });
    const byStrategy = new Map(backtests.map((b) => [b.strategyId, b]));
    return strategies.map((strategy) => ({ strategy, backtest: byStrategy.get(strategy.id) ?? null }));
  }

  /**
   * Manually promote a specific strategy version to LIVE for its ticker, retiring the currently-live
   * one (flattening its open positions first) so exactly one strategy is ever live per ticker. A
   * deliberate operator override — it bypasses the evaluation gate.
   */
  async activateStrategy(strategyId: string) {
    const strategy = await this.strategyRepo.findOne({ where: { id: strategyId } });
    if (!strategy) throw new NotFoundException('Strategy not found');

    const backtestFor = (id: string) => this.backtestRepo.findOne({ where: { strategyId: id } });
    if (strategy.status === StrategyStatus.LIVE) {
      return { strategy, backtest: await backtestFor(strategy.id) };
    }

    const { tickerId } = strategy;
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });

    const previousLive = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
    if (previousLive && previousLive.id !== strategy.id) {
      // Orphan-prevention: flatten open positions before the switch.
      await this.executionService
        .flattenOpenPositionsForTicker(tickerId, `manual activation: retiring v${previousLive.version} for v${strategy.version}`)
        .catch((err) => this.logger.warn(`Flatten-on-switch failed (non-fatal): ${err.message}`));
      await this.strategyRepo.update({ tickerId, status: StrategyStatus.LIVE }, { status: StrategyStatus.RETIRED });
    }

    strategy.status = StrategyStatus.LIVE;
    await this.strategyRepo.save(strategy);
    if (ticker) {
      ticker.status = TickerStatus.ACTIVE;
      ticker.onboardingStage = OnboardingStage.READY;
      await this.tickerRepo.save(ticker);
    }
    return { strategy, backtest: await backtestFor(strategy.id) };
  }

  async getPolicies() {
    return this.policyRepo.find();
  }
}
