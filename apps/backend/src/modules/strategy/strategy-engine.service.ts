import { Injectable, NotFoundException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Strategy, StrategyStatus, ExecutionMode } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { LiveVsBacktestDivergence } from '../../entities/live-vs-backtest-divergence.entity';
import { Position, PositionStatus } from '../../entities/position.entity';
import { LlmService } from '../llm/llm.service';
import { LlmPurpose } from '../../entities/llm-cost-log.entity';
import { StrategyEvaluatorService } from './strategy-evaluator.service';
import { AiLessonsService } from './ai-lessons.service';

const DIVERGENCE_FLAG_THRESHOLD_PCT = 30; // spec 7.5 — significant divergence triggers regeneration

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
    @InjectRepository(OhlcvData)
    private readonly ohlcvRepo: Repository<OhlcvData>,
    @InjectRepository(LiveVsBacktestDivergence)
    private readonly divergenceRepo: Repository<LiveVsBacktestDivergence>,
    @InjectRepository(Position)
    private readonly positionRepo: Repository<Position>,
    private readonly evaluatorService: StrategyEvaluatorService,
    private readonly llmService: LlmService,
    private readonly aiLessonsService: AiLessonsService,
  ) {}

  async generateStrategyForTicker(tickerId: string, retirementReason = 'new strategy generation cycle') {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId }, relations: ['marketType'] });
    if (!ticker) throw new NotFoundException('Ticker not found');

    ticker.onboardingStage = OnboardingStage.GENERATING_STRATEGY;
    await this.tickerRepo.save(ticker);

    const candles = await this.ohlcvRepo.find({
      where: { tickerId },
      order: { timestamp: 'ASC' },
      take: 1000,
    });

    // Spec 4.11.2: retrieve relevant past lessons before generating, so the LLM doesn't
    // repeat a documented mistake for this ticker (or a similar strategy type).
    const priorStrategy = await this.strategyRepo.findOne({ where: { tickerId }, order: { version: 'DESC' } });
    const lessons = await this.aiLessonsService.retrieveRelevantLessons(
      tickerId,
      priorStrategy ? this.inferStrategyTypeTag(priorStrategy.parametersJson) : undefined,
    );
    const lessonsBlock = this.aiLessonsService.formatLessonsForPrompt(lessons);

    const latestPrice = candles.length > 0 ? Number(candles[candles.length - 1].close) : 0;
    const summaryPrompt = `Analyze ticker ${ticker.symbol} (Interval: ${ticker.interval}, Latest Price: ${latestPrice}).
Propose optimal quantitative indicator parameters for an automated trend-following trading strategy. Respond in JSON.

Relevant lessons from past strategies on this ticker/strategy type (steer away from documented failures, keep
successful approaches in mind):
${lessonsBlock}`;

    const llmRes = await this.llmService.generateCompletion(summaryPrompt);
    let parsedParams: any;
    try {
      parsedParams = JSON.parse(llmRes.content);
    } catch {
      parsedParams = {
        indicatorConfig: { emaFastPeriod: 12, emaSlowPeriod: 26, stopLossPct: 1.5, takeProfitPct: 3.5 },
      };
    }

    const existingCount = await this.strategyRepo.count({ where: { tickerId } });
    const strategy = this.strategyRepo.create({
      tickerId,
      version: existingCount + 1,
      status: StrategyStatus.DRAFT,
      executionMode: ExecutionMode.MODE_A_RULES,
      parametersJson: parsedParams,
      generatedBy: llmRes.model,
    });
    await this.strategyRepo.save(strategy);

    await this.llmService.logCost(tickerId, strategy.id, LlmPurpose.STRATEGY_GENERATION, llmRes);

    ticker.onboardingStage = OnboardingStage.BACKTESTING;
    await this.tickerRepo.save(ticker);

    let activePolicy = await this.policyRepo.findOne({ where: { isActive: true } });
    if (!activePolicy) {
      activePolicy = this.policyRepo.create({
        name: 'Default Policy',
        minSharpe: 1.0,
        maxDrawdownPct: 20.0,
        minProfitFactor: 1.3,
        minTradeCount: 100,
      });
    }

    const evalResult = this.evaluatorService.evaluateStrategy(candles, parsedParams, activePolicy);

    ticker.onboardingStage = OnboardingStage.EVALUATING;
    await this.tickerRepo.save(ticker);

    const backtest = this.backtestRepo.create({
      strategyId: strategy.id,
      sharpe: evalResult.sharpe,
      sortino: evalResult.sortino,
      calmar: evalResult.calmar,
      maxDrawdown: evalResult.maxDrawdown,
      drawdownDuration: evalResult.drawdownDuration,
      profitFactor: evalResult.profitFactor,
      tradeCount: evalResult.tradeCount,
      monteCarloSummaryJson: evalResult.monteCarloSummary,
      regimeBreakdownJson: evalResult.regimeBreakdown,
      passedEvaluationGate: evalResult.passedEvaluationGate,
    });
    await this.backtestRepo.save(backtest);

    if (evalResult.passedEvaluationGate) {
      // Retire previous LIVE strategy for this ticker — and record what we learned
      // from it before it disappears from the "current" view (spec 4.11.1).
      const previousLive = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
      if (previousLive) {
        const previousBacktest = await this.backtestRepo.findOne({ where: { strategyId: previousLive.id } });
        const latestDivergence = await this.divergenceRepo.findOne({
          where: { strategyId: previousLive.id },
          order: { measuredAt: 'DESC' },
        });
        await this.aiLessonsService
          .recordLesson({
            retiredStrategy: previousLive,
            retiredBacktest: previousBacktest,
            divergence: latestDivergence,
            ticker,
            reason: retirementReason,
          })
          .catch((err) => this.logger.warn(`Lesson recording failed (non-fatal): ${err.message}`));
      }

      await this.strategyRepo.update({ tickerId, status: StrategyStatus.LIVE }, { status: StrategyStatus.RETIRED });

      strategy.status = StrategyStatus.LIVE;
      await this.strategyRepo.save(strategy);

      ticker.status = TickerStatus.ACTIVE;
      ticker.onboardingStage = OnboardingStage.READY;
      await this.tickerRepo.save(ticker);
    } else {
      strategy.status = StrategyStatus.EVALUATED;
      await this.strategyRepo.save(strategy);

      ticker.onboardingStage = OnboardingStage.FAILED;
      await this.tickerRepo.save(ticker);
    }

    return { strategy, backtest, evaluation: evalResult };
  }

  async evaluateRulesSignal(tickerId: string, currentCandle: OhlcvData): Promise<'BUY' | 'SELL' | 'HOLD'> {
    const liveStrategy = await this.strategyRepo.findOne({
      where: { tickerId, status: StrategyStatus.LIVE },
    });

    if (!liveStrategy) return 'HOLD';

    if (liveStrategy.executionMode === ExecutionMode.MODE_A_RULES) {
      const params = liveStrategy.parametersJson;
      const emaFast = params.indicatorConfig?.emaFastPeriod || 12;
      const emaSlow = params.indicatorConfig?.emaSlowPeriod || 26;

      const candles = await this.ohlcvRepo.find({
        where: { tickerId },
        order: { timestamp: 'DESC' },
        take: emaSlow + 5,
      });

      if (candles.length < emaSlow) return 'HOLD';

      const closes = candles.map((c) => Number(c.close)).reverse();
      const last = closes[closes.length - 1];
      const prev = closes[closes.length - 2];

      if (last > prev * 1.002) return 'BUY';
      if (last < prev * 0.998) return 'SELL';
      return 'HOLD';
    }

    return 'HOLD';
  }

  /**
   * Daily re-evaluation (spec 2.5/7.5/10): compares each LIVE strategy's actual closed-
   * trade profit factor since going live against its backtest-expected profit factor.
   * Significant divergence gets flagged and immediately triggers regeneration — this is
   * the "don't wait for manual approval" auto-replace path the spec calls for.
   */
  async runDivergenceCheckForAllLiveStrategies(): Promise<{ checked: number; flagged: number; regenerated: number }> {
    const liveStrategies = await this.strategyRepo.find({ where: { status: StrategyStatus.LIVE } });
    let flagged = 0;
    let regenerated = 0;

    for (const strategy of liveStrategies) {
      const backtest = await this.backtestRepo.findOne({ where: { strategyId: strategy.id } });
      if (!backtest) continue;

      const closedPositions = await this.positionRepo.find({
        where: { strategyId: strategy.id, status: PositionStatus.CLOSED },
      });
      if (closedPositions.length < 5) continue; // not enough live trades yet to judge

      const grossProfit = closedPositions.filter((p) => Number(p.realizedPl) > 0).reduce((s, p) => s + Number(p.realizedPl), 0);
      const grossLoss = Math.abs(
        closedPositions.filter((p) => Number(p.realizedPl) <= 0).reduce((s, p) => s + Number(p.realizedPl), 0),
      );
      const liveProfitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 3.0 : 0;

      const expected = Number(backtest.profitFactor);
      const divergencePct = expected !== 0 ? (Math.abs(liveProfitFactor - expected) / Math.abs(expected)) * 100 : 100;
      const isFlagged = divergencePct >= DIVERGENCE_FLAG_THRESHOLD_PCT;

      await this.divergenceRepo.save(
        this.divergenceRepo.create({
          strategyId: strategy.id,
          backtestExpectedMetric: expected,
          liveActualMetric: Number(liveProfitFactor.toFixed(4)),
          divergencePct: Number(divergencePct.toFixed(2)),
          flagged: isFlagged,
        }),
      );

      if (isFlagged) {
        flagged++;
        this.logger.warn(
          `Strategy ${strategy.id} (ticker ${strategy.tickerId}) flagged: live profit factor ${liveProfitFactor.toFixed(2)} vs backtest ${expected.toFixed(2)} (${divergencePct.toFixed(1)}% divergence). Triggering regeneration.`,
        );
        await this.generateStrategyForTicker(
          strategy.tickerId,
          `live-vs-backtest divergence ${divergencePct.toFixed(1)}% exceeded ${DIVERGENCE_FLAG_THRESHOLD_PCT}% threshold`,
        );
        regenerated++;
      }
    }

    return { checked: liveStrategies.length, flagged, regenerated };
  }

  async getActiveStrategyForTicker(tickerId: string) {
    const strategy = await this.strategyRepo.findOne({
      where: { tickerId, status: StrategyStatus.LIVE },
    });
    if (!strategy) return null;

    const backtest = await this.backtestRepo.findOne({ where: { strategyId: strategy.id } });
    return { strategy, backtest };
  }

  async getStrategiesByTicker(tickerId: string) {
    return this.strategyRepo.find({ where: { tickerId }, order: { version: 'DESC' } });
  }

  async getPolicies() {
    return this.policyRepo.find();
  }

  private inferStrategyTypeTag(params: Record<string, any>): string {
    if (params?.indicatorConfig?.rsiPeriod) return 'ema_rsi_trend';
    if (params?.indicatorConfig?.emaFastPeriod) return 'ema_crossover';
    return 'unclassified';
  }
}
