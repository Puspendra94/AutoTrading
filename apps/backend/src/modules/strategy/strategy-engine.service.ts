import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
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
import { StrategyParamsSchema } from '../llm/schemas/strategy-params.schema';
import { LiveDecisionSchema } from '../llm/schemas/live-decision.schema';
import { MarketDataService } from '../market-data/market-data.service';

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
    private readonly marketDataService: MarketDataService,
  ) {}

  async generateStrategyForTicker(tickerId: string, retirementReason = 'new strategy generation cycle') {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId }, relations: ['marketType'] });
    if (!ticker) throw new NotFoundException('Ticker not found');

    // Spec 4.7 — refuse to proceed if the underlying data has unresolved quality flags
    // (gaps/spikes) rather than silently generating a strategy off suspect history.
    const hasBlockingFlags = await this.marketDataService.hasUnresolvedBlockingFlags(tickerId);
    if (hasBlockingFlags) {
      ticker.onboardingStage = OnboardingStage.FAILED;
      await this.tickerRepo.save(ticker);
      throw new BadRequestException(
        `Strategy generation refused for ticker ${tickerId}: unresolved data quality flags (gap/spike) exist. Resolve or acknowledge them before generating.`,
      );
    }

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
Propose optimal quantitative indicator parameters for an automated trend-following trading strategy: EMA
fast/slow crossover periods, optional RSI filter, and stop-loss/take-profit percentages.

Respond in JSON with exactly these fields: strategyName (string), indicatorConfig.emaFastPeriod (integer, candles),
indicatorConfig.emaSlowPeriod (integer, candles), indicatorConfig.rsiPeriod (integer, optional),
indicatorConfig.rsiBuyThreshold (0-100, optional), indicatorConfig.rsiSellThreshold (0-100, optional),
indicatorConfig.stopLossPct (percent, e.g. 1.5), indicatorConfig.takeProfitPct (percent, e.g. 3.5), and
reasoning (string).

Relevant lessons from past strategies on this ticker/strategy type (steer away from documented failures, keep
successful approaches in mind):
${lessonsBlock}`;

    // Forced into the exact shape strategy-evaluator.service.ts and ai-lessons.service.ts
    // read (indicatorConfig.emaFastPeriod/emaSlowPeriod/stopLossPct/takeProfitPct/rsiPeriod)
    // via LangChain's withStructuredOutput — regardless of which provider in LLM_MODELS
    // answers, so a generic "respond in JSON" prompt can no longer drift into a
    // provider-specific shape that silently gets ignored downstream. The field list above
    // is also spelled out in prose since DeepSeek's reasoning models are forced onto the
    // 'jsonMode' method (see llm.service.ts), which sends no structural schema to the model.
    const llmRes = await this.llmService.generateStructuredCompletion(summaryPrompt, StrategyParamsSchema, {
      schemaName: 'propose_strategy_params',
    });
    const parsedParams = llmRes.data;

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

    await this.llmService.logCost(tickerId, strategy.id, LlmPurpose.STRATEGY_GENERATION, {
      content: JSON.stringify(parsedParams),
      inputTokens: llmRes.inputTokens,
      outputTokens: llmRes.outputTokens,
      costUsd: llmRes.costUsd,
      model: llmRes.model,
      provider: llmRes.provider,
    });

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
      parameterCount: evalResult.parameterCount,
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

  /**
   * Single live-signal entry point for the strategy's live status — dispatches to Mode
   * A (deterministic rules engine, zero LLM calls in the live path per spec 2.6) or
   * Mode B (AI live-decision, per-decision LLM call) based on the live strategy's
   * executionMode. Called by MarketStreamService on every final candle close — this
   * closes the gap where a promoted LIVE strategy never actually placed a live trade
   * on its own; only manual/UI-triggered orders exercised the execution path before.
   */
  async evaluateLiveSignal(tickerId: string, currentCandle: { close: number }): Promise<{ signal: 'BUY' | 'SELL' | 'HOLD'; strategyId?: string }> {
    const liveStrategy = await this.strategyRepo.findOne({
      where: { tickerId, status: StrategyStatus.LIVE },
    });
    if (!liveStrategy) return { signal: 'HOLD' };

    if (liveStrategy.executionMode === ExecutionMode.MODE_B_AI_LIVE) {
      const signal = await this.evaluateModeBLiveDecision(tickerId, liveStrategy, currentCandle);
      return { signal, strategyId: liveStrategy.id };
    }

    const signal = await this.evaluateRulesSignal(tickerId, liveStrategy);
    return { signal, strategyId: liveStrategy.id };
  }

  /**
   * Mode A (spec 2.6) — deterministic, zero-LLM-call EMA fast/slow crossover with
   * stop-loss/take-profit exits, mirroring exactly the logic strategy-evaluator.service.ts
   * backtests (previously this used an unrelated naive momentum-threshold check, so what
   * was promoted based on the backtest never matched what actually executed live).
   */
  private async evaluateRulesSignal(tickerId: string, liveStrategy: Strategy): Promise<'BUY' | 'SELL' | 'HOLD'> {
    const params = liveStrategy.parametersJson;
    const emaFastPeriod = params.indicatorConfig?.emaFastPeriod || 12;
    const emaSlowPeriod = params.indicatorConfig?.emaSlowPeriod || 26;
    const stopLossPct = (params.indicatorConfig?.stopLossPct || 1.5) / 100;
    const takeProfitPct = (params.indicatorConfig?.takeProfitPct || 3.5) / 100;

    const candles = await this.ohlcvRepo.find({
      where: { tickerId },
      order: { timestamp: 'DESC' },
      take: emaSlowPeriod + 5,
    });
    if (candles.length < emaSlowPeriod + 2) return 'HOLD';

    const closes = candles.map((c) => Number(c.close)).reverse();
    const fastEma = this.calculateEma(closes, emaFastPeriod);
    const slowEma = this.calculateEma(closes, emaSlowPeriod);
    const i = closes.length - 1;
    const price = closes[i];

    const openPosition = await this.positionRepo.findOne({
      where: { tickerId, strategyId: liveStrategy.id, status: PositionStatus.OPEN },
    });

    if (!openPosition) {
      const bullishCross = fastEma[i] > slowEma[i] && fastEma[i - 1] <= slowEma[i - 1];
      return bullishCross ? 'BUY' : 'HOLD';
    }

    const entryPrice = Number(openPosition.entryPrice);
    const returnPct = (price - entryPrice) / entryPrice;
    const isStopLoss = returnPct <= -stopLossPct;
    const isTakeProfit = returnPct >= takeProfitPct;
    const isCrossDown = fastEma[i] < slowEma[i];

    return isStopLoss || isTakeProfit || isCrossDown ? 'SELL' : 'HOLD';
  }

  private calculateEma(prices: number[], period: number): number[] {
    const ema: number[] = new Array(prices.length).fill(0);
    const k = 2 / (period + 1);
    ema[0] = prices[0];
    for (let i = 1; i < prices.length; i++) {
      ema[i] = prices[i] * k + ema[i - 1] * (1 - k);
    }
    return ema;
  }

  /**
   * Mode B (spec 2.6/4.4) — AI live-decision execution for tickers explicitly flagged
   * for it. Per spec 2.7/9.3, the LLM only ever sees summarized structured statistics
   * (never raw candle history), and relevant lessons (spec 4.11.2) are included to
   * steer it away from previously-documented failures for this ticker/strategy type.
   */
  private async evaluateModeBLiveDecision(tickerId: string, liveStrategy: Strategy, currentCandle: { close: number }): Promise<'BUY' | 'SELL' | 'HOLD'> {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    if (!ticker) return 'HOLD';

    const openPosition = await this.positionRepo.findOne({
      where: { tickerId, strategyId: liveStrategy.id, status: PositionStatus.OPEN },
    });

    const recentCandles = await this.ohlcvRepo.find({ where: { tickerId }, order: { timestamp: 'DESC' }, take: 20 });
    const closes = recentCandles.map((c) => Number(c.close)).reverse();
    const priceChangePct = closes.length >= 2 ? ((closes[closes.length - 1] - closes[0]) / closes[0]) * 100 : 0;

    const lessons = await this.aiLessonsService.retrieveRelevantLessons(tickerId, this.inferStrategyTypeTag(liveStrategy.parametersJson));
    const lessonsBlock = this.aiLessonsService.formatLessonsForPrompt(lessons);

    const positionBlock = openPosition
      ? `Currently holding a position: entry price ${Number(openPosition.entryPrice)}, unrealized P/L ${Number(openPosition.unrealizedPl)}.`
      : 'Currently flat (no open position).';

    const prompt = `Live trading decision for ${ticker.symbol} (Interval: ${ticker.interval}).
Latest close: ${Number(currentCandle.close)}. Price change over last ${closes.length} candles: ${priceChangePct.toFixed(2)}%.
${positionBlock}
Strategy context: ${JSON.stringify(liveStrategy.parametersJson)}.

Relevant lessons from past strategies on this ticker/strategy type:
${lessonsBlock}

Decide the trading action right now: BUY (enter/add long), SELL (exit an open position), or HOLD (do nothing).
Respond in JSON with your decision.`;

    const llmRes = await this.llmService.generateStructuredCompletion(prompt, LiveDecisionSchema, {
      schemaName: 'propose_live_decision',
      maxTokens: 512,
    });

    await this.llmService.logCost(tickerId, liveStrategy.id, LlmPurpose.LIVE_DECISION, {
      content: JSON.stringify(llmRes.data),
      inputTokens: llmRes.inputTokens,
      outputTokens: llmRes.outputTokens,
      costUsd: llmRes.costUsd,
      model: llmRes.model,
      provider: llmRes.provider,
    });

    // Never let the AI open a second position (spec 5.2 is enforced at the gate
    // regardless, but avoid the pointless round-trip) or exit one that doesn't exist.
    if (llmRes.data.action === 'BUY' && openPosition) return 'HOLD';
    if (llmRes.data.action === 'SELL' && !openPosition) return 'HOLD';
    return llmRes.data.action;
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
