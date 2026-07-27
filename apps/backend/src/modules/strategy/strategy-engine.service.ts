import { Injectable, NotFoundException, BadRequestException, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { In, Repository } from 'typeorm';
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
import { StrategyPerformanceService } from './strategy-performance.service';
import { AiLessonsService } from './ai-lessons.service';
import { StrategyGenSchema } from '../llm/schemas/strategy-gen.schema';
import { LiveDecisionSchema, ExitTightenDecisionSchema } from '../llm/schemas/live-decision.schema';
import { GenerationPlanSchema } from '../llm/schemas/generation-plan.schema';
import { MarketDataService } from '../market-data/market-data.service';
import { ExecutionService } from '../risk-execution/execution.service';
import { resolveIR, warmupBars, validateIR, strategyTypeTag, StrategyIR } from './dsl/strategy-ir';
import { STRATEGY_GRAMMAR_PROMPT } from './dsl/grammar-prompt';
import { shouldEnter, exitReason, intendedPositionState } from './dsl/interpreter';
import { config } from '../../config/configuration';

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
    private readonly performanceService: StrategyPerformanceService,
    private readonly llmService: LlmService,
    private readonly aiLessonsService: AiLessonsService,
    private readonly marketDataService: MarketDataService,
    private readonly executionService: ExecutionService,
  ) {}

  async generateStrategyForTicker(
    tickerId: string,
    retirementReason = 'new strategy generation cycle',
    intervalOverride?: string,
    skipGate = false,
  ) {
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

    // Base policy — the thresholds the AI planner may tune WITHIN hard floors; maxParameterCount
    // stays fixed and is never AI-controlled.
    let activePolicy = await this.policyRepo.findOne({ where: { isActive: true } });
    if (!activePolicy) {
      activePolicy = this.policyRepo.create({
        name: 'Default Policy',
        minSharpe: 1.0,
        maxDrawdownPct: 20.0,
        minProfitFactor: 1.3,
        minTradeCount: 8,
      });
    }

    // ADAPTIVE LOOP: (1) record how the CURRENT live strategy is really doing (feeds memory),
    // then (2) let the meta-planner read ALL memory — failures + live-performance insights — and
    // decide HOW to build the next one: interval, data window, gate strictness (clamped to safe
    // floors), and which signals to emphasize. Both are non-fatal to the generation itself.
    await this.recordLiveInsightForCurrentStrategy(ticker).catch((err) =>
      this.logger.warn(`Live-insight recording failed (non-fatal): ${err.message}`),
    );
    const plan = await this.planGeneration(ticker, activePolicy);
    // An explicit user-chosen timeframe overrides the planner's interval pick (and enables fast
    // sub-hour intervals the planner enum doesn't offer). Validated against the '<n><m|h|d|w>' shape.
    const evalInterval =
      intervalOverride && /^(\d+)([mhdw])$/i.test(intervalOverride) ? intervalOverride : plan.interval;
    const effectivePolicy = plan.policy;

    // Backtest on the PLANNED timeframe (aggregated from the 1m base via TimescaleDB time_bucket),
    // over the planned number of bars. 1m is noise/fee-dominated for trend following; higher
    // intervals trade fewer, cleaner swings. Normalize raw-query rows into the evaluator's shape.
    const rawCandles = await this.marketDataService.getCandlesForInterval(tickerId, evalInterval, plan.candleLimit);
    const candles = rawCandles.map((c: any) => ({
      ...c,
      timestamp: c.timestamp instanceof Date ? c.timestamp : new Date(c.timestamp),
      close: Number(c.close),
    }));

    // Spec 4.11.2: retrieve relevant past lessons before generating, so the LLM doesn't
    // repeat a documented mistake for this ticker (or a similar strategy type).
    const priorStrategy = await this.strategyRepo.findOne({ where: { tickerId }, order: { version: 'DESC' } });
    const lessons = await this.aiLessonsService.retrieveRelevantLessons(
      tickerId,
      priorStrategy ? this.inferStrategyTypeTag(priorStrategy.parametersJson) : undefined,
    );
    const lessonsBlock = this.aiLessonsService.formatLessonsForPrompt(lessons);

    const latestPrice = candles.length > 0 ? Number(candles[candles.length - 1].close) : 0;
    const paramBudget = Number((effectivePolicy as any).maxParameterCount ?? activePolicy.maxParameterCount ?? 8);
    // Short strategies are only valid on futures markets; spot stays long-only (validateIR would
    // reject a short on spot in the caller regardless, but steer the LLM correctly up front).
    const allowShort = ticker.marketType?.name?.toLowerCase() === 'futures';
    const directionClause = allowShort
      ? `MARKET TYPE: FUTURES — you MAY compose a SHORT strategy instead of a long. To go short, set "direction":"short": "entry" then SELLS to open (the trade profits when price FALLS) and "exit" BUYS to close; stop-loss / take-profit / trailing / profit-floor all mirror automatically. In a clear downtrend a short is usually better than sitting flat. For a long, set "direction":"long" or omit it — pick whichever the market and lessons favor.`
      : `MARKET TYPE: SPOT — LONG ONLY. Do NOT set "direction":"short" (you can only buy then sell).`;
    const summaryPrompt = `Design an automated trading strategy for ${ticker.symbol} (Interval: ${evalInterval}, Latest Price: ${latestPrice}).

${STRATEGY_GRAMMAR_PROMPT}

${directionClause}

Parameter budget for THIS strategy: at most ${paramBudget} distinct tunable knobs.
Timeframe: size indicator periods and stop/target to a ${evalInterval} bar — give trades room for a real multi-bar swing rather than reacting to single-bar noise. Prioritize a positive out-of-sample Sharpe (>= 1) and profit factor (>= 1.3) over trade frequency.
Move decisively away from any approach the lessons below show failing (low/negative Sharpe or profit factor under 1) — do NOT repeat a documented failure; try a different indicator family or structure when the lessons point that way.
${plan.signalEmphasis ? `\nPlanner emphasis for this cycle (follow it): ${plan.signalEmphasis}\n` : ''}
Relevant lessons from past strategies on this ticker/strategy type (steer away from documented failures, keep successful approaches in mind):
${lessonsBlock}

Respond with strategyName, reasoning, and the entry, exit, and risk fields (JSON objects, not strings).`;

    ticker.onboardingStage = OnboardingStage.BACKTESTING;
    await this.tickerRepo.save(ticker);

    // Gate-before-save: a generated strategy is persisted ONLY if its out-of-sample
    // backtest clears the active policy gate. Retry the LLM up to MAX_ATTEMPTS; failed
    // attempts are discarded (never written to `strategies`), though their real LLM cost
    // is still logged so spend stays truthful.
    const MAX_ATTEMPTS = 3;
    let lastEval: any = null;
    let lastParams: any = null;
    let llmFailures = 0;

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      let llmRes;
      try {
        // Generous token budget: DeepSeek's reasoning models spend a big chunk "thinking" before
        // emitting the JSON rule tree, so a tight cap truncates it -> parse fails -> the whole
        // attempt is wasted. 4k keeps the structured output intact.
        llmRes = await this.llmService.generateStructuredCompletion(summaryPrompt, StrategyGenSchema, {
          schemaName: 'propose_strategy',
          maxTokens: 4000,
        });
      } catch (err: any) {
        llmFailures++;
        this.logger.warn(`Strategy attempt ${attempt}/${MAX_ATTEMPTS} — LLM call failed: ${err.message}. Discarding.`);
        continue;
      }

      await this.llmService.logCost(tickerId, null, LlmPurpose.STRATEGY_GENERATION, {
        content: JSON.stringify(llmRes.data),
        inputTokens: llmRes.inputTokens,
        outputTokens: llmRes.outputTokens,
        costUsd: llmRes.costUsd,
        model: llmRes.model,
        provider: llmRes.provider,
      });

      // Parse the rule-tree string into an IR and validate it structurally BEFORE backtesting.
      // A malformed/invalid tree is a failed attempt (discarded); retry within MAX_ATTEMPTS.
      const ir = this.parseGeneratedStrategy(llmRes.data);
      if (!ir) {
        this.logger.warn(
          `Strategy attempt ${attempt}/${MAX_ATTEMPTS} for ticker ${tickerId} produced an invalid rule tree. Discarding.`,
        );
        lastParams = llmRes.data;
        continue;
      }
      if (ir.direction === 'short' && !allowShort) {
        // A short on a spot market is invalid — discard and retry rather than promote something
        // that could never be executed on this venue.
        this.logger.warn(
          `Strategy attempt ${attempt}/${MAX_ATTEMPTS} for ticker ${tickerId} proposed a SHORT on a non-futures market. Discarding.`,
        );
        lastParams = llmRes.data;
        continue;
      }

      const evalResult = this.evaluatorService.evaluateStrategy(candles, ir, effectivePolicy);
      lastEval = evalResult;
      lastParams = ir;

      const gateBypassed = skipGate && !evalResult.passedEvaluationGate;
      if (!evalResult.passedEvaluationGate && !skipGate) {
        const failing = this.gateFailureReasons(evalResult, effectivePolicy);
        this.logger.warn(
          `Strategy attempt ${attempt}/${MAX_ATTEMPTS} for ticker ${tickerId} failed the gate. ` +
            `Failing conditions: ${failing.join('; ') || 'unknown'}. ` +
            `(sharpe ${evalResult.sharpe}, PF ${evalResult.profitFactor}, DD ${evalResult.maxDrawdown}%, ` +
            `trades ${evalResult.tradeCount}, params ${evalResult.parameterCount}). Discarding.`,
        );
        continue;
      }
      if (gateBypassed) {
        this.logger.warn(
          `TESTING: gate BYPASSED for ticker ${tickerId} — promoting a strategy that FAILED the gate ` +
            `(sharpe ${evalResult.sharpe}, PF ${evalResult.profitFactor}, trades ${evalResult.tradeCount}). ` +
            `Use only for exercising the execution loop.`,
        );
      }

      // --- Passed the gate (or bypassed for testing): persist strategy + backtest, then promote. ---
      const existingCount = await this.strategyRepo.count({ where: { tickerId } });
      const strategy = this.strategyRepo.create({
        tickerId,
        version: existingCount + 1,
        status: StrategyStatus.DRAFT,
        executionMode: ExecutionMode.MODE_A_RULES,
        parametersJson: ir,
        evalInterval,
        generatedBy: llmRes.model,
      });
      await this.strategyRepo.save(strategy);

      const backtest = this.backtestRepo.create({
        strategyId: strategy.id,
        sharpe: evalResult.sharpe,
        sortino: evalResult.sortino,
        calmar: evalResult.calmar,
        maxDrawdown: evalResult.maxDrawdown,
        drawdownDuration: evalResult.drawdownDuration,
        profitFactor: evalResult.profitFactor,
        tradeCount: evalResult.tradeCount,
        totalReturnPct: evalResult.totalReturnPct,
        winRate: evalResult.winRate,
        monteCarloSummaryJson: evalResult.monteCarloSummary,
        regimeBreakdownJson: evalResult.regimeBreakdown,
        parameterCount: evalResult.parameterCount,
        // Truthful: reflects the real gate result even when promoted via the testing bypass.
        passedEvaluationGate: evalResult.passedEvaluationGate,
      });
      await this.backtestRepo.save(backtest);

      ticker.onboardingStage = OnboardingStage.EVALUATING;
      await this.tickerRepo.save(ticker);

      // Retire the previous LIVE strategy (record its lesson first) and promote this one,
      // keeping exactly one strategy live per ticker.
      const previousLive = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
      if (previousLive && previousLive.id !== strategy.id) {
        // Close any position the outgoing strategy left open BEFORE switching, so the incoming
        // strategy never inherits an orphan it won't manage (exits are scoped to its own
        // strategyId). Done first so the just-closed trades are reflected in the lesson below.
        await this.executionService
          .flattenOpenPositionsForTicker(tickerId, `strategy switch: retiring v${previousLive.version} for v${strategy.version}`)
          .catch((err) => this.logger.warn(`Flatten-on-switch failed (non-fatal): ${err.message}`));

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
        await this.strategyRepo.update({ tickerId, status: StrategyStatus.LIVE }, { status: StrategyStatus.RETIRED });
      }

      strategy.status = StrategyStatus.LIVE;
      await this.strategyRepo.save(strategy);

      ticker.status = TickerStatus.ACTIVE;
      ticker.onboardingStage = OnboardingStage.READY;
      await this.tickerRepo.save(ticker);

      // Replay the strategy over full real history in the background (non-blocking).
      this.performanceService.computeForStrategyInBackground(strategy.id);

      return { strategy, backtest, evaluation: evalResult, saved: true, attempts: attempt, gateBypassed };
    }

    // No attempt cleared the gate — nothing is persisted, but record WHY so the next
    // generation cycle can learn from it (spec 4.11 continuous learning on the failure path).
    ticker.onboardingStage = OnboardingStage.FAILED;
    await this.tickerRepo.save(ticker);
    const lastFailures = lastEval ? this.gateFailureReasons(lastEval, effectivePolicy) : [];
    if (lastEval && lastParams) {
      // Fire-and-forget: the lesson-summary LLM call is slow (reasoning models), and blocking the
      // response on it can push the whole request past the HTTP timeout. Record it in the
      // background — it only needs to be persisted before the NEXT generation, not this response.
      void this.aiLessonsService
        .recordFailureLesson({
          ticker,
          attemptedParams: lastParams,
          evaluation: lastEval,
          failingConditions: lastFailures,
          reason: retirementReason,
        })
        .catch((err) => this.logger.warn(`Failure-lesson recording failed (non-fatal): ${err.message}`));
    }
    return {
      strategy: null,
      backtest: null,
      evaluation: lastEval,
      params: lastParams,
      saved: false,
      attempts: MAX_ATTEMPTS,
      failingConditions: lastFailures,
      message:
        llmFailures === MAX_ATTEMPTS
          ? `The LLM was unavailable — all ${MAX_ATTEMPTS} generation attempts failed to get a response (check the provider/API key/credits in LLM_MODELS). No strategy was created.`
          : `No generated strategy passed the evaluation gate after ${MAX_ATTEMPTS} attempts.` +
            (lastFailures.length ? ` Last attempt failed on: ${lastFailures.join('; ')}.` : ''),
    };
  }

  /**
   * Human-readable list of which policy conditions a backtest failed — purely for logging,
   * so a discarded attempt reports *why* it was rejected instead of a bare "failed the gate".
   * Mirrors the exact conditions in StrategyEvaluatorService.evaluateStrategy (out-of-sample
   * metrics vs. the active policy).
   */
  private gateFailureReasons(ev: any, policy: any): string[] {
    const reasons: string[] = [];
    if (ev.sharpe < Number(policy.minSharpe)) {
      reasons.push(`sharpe ${ev.sharpe} < minSharpe ${Number(policy.minSharpe)}`);
    }
    if (ev.maxDrawdown > Number(policy.maxDrawdownPct)) {
      reasons.push(`maxDrawdown ${ev.maxDrawdown}% > maxDrawdownPct ${Number(policy.maxDrawdownPct)}%`);
    }
    if (ev.profitFactor < Number(policy.minProfitFactor)) {
      reasons.push(`profitFactor ${ev.profitFactor} < minProfitFactor ${Number(policy.minProfitFactor)}`);
    }
    if (ev.tradeCount < Number(policy.minTradeCount)) {
      reasons.push(`tradeCount ${ev.tradeCount} < minTradeCount ${Number(policy.minTradeCount)}`);
    }
    if (ev.parameterCount > Number(policy.maxParameterCount)) {
      reasons.push(`parameterCount ${ev.parameterCount} > maxParameterCount ${Number(policy.maxParameterCount)}`);
    }
    return reasons;
  }

  /**
   * Meta-planner (adaptive loop): before building a strategy, read ALL accumulated memory
   * (failed-generation lessons + live-performance insights) and decide HOW to build the next
   * one — timeframe, data window, gate strictness, and which signals to emphasize. Proposed gate
   * thresholds are CLAMPED to hard safe floors so the generator can never weaken its own test
   * into meaninglessness. Falls back to the static config defaults if planning is unavailable.
   */
  private async planGeneration(
    ticker: Ticker,
    activePolicy: StrategyEvaluationPolicy,
  ): Promise<{ interval: string; candleLimit: number; policy: any; signalEmphasis: string; reasoning: string }> {
    const defaultPlan = {
      interval: config.strategy.evalInterval,
      candleLimit: config.strategy.evalCandleLimit,
      policy: activePolicy,
      signalEmphasis: '',
      reasoning: 'default plan (planner unavailable / no lessons yet)',
    };

    const lessons = await this.aiLessonsService.retrieveRelevantLessons(ticker.id, undefined, 12);
    if (lessons.length === 0) return defaultPlan; // nothing to adapt from yet
    const lessonsBlock = this.aiLessonsService.formatLessonsForPrompt(lessons);

    const planPrompt = `You are planning HOW to generate the next automated trading strategy for ${ticker.symbol}, BEFORE it is built. The generator can compose ANY rule tree from these indicator families: moving averages (EMA/SMA/MACD), oscillators (RSI/Stochastic), volatility/channels (Bollinger/ATR/Donchian), and price levels (rolling highs/lows) — trend-following OR mean-reversion OR combinations, not just EMA crossovers. The lessons below include failed generation attempts AND reviews of how live strategies actually performed. Choose the setup most likely to produce a strategy that clears the gate and holds up. Base every choice on the lessons: too few trades -> shorter interval or more candles; a whole approach repeatedly failing (e.g. plain MA crossovers whipsawing) -> steer the generator toward a DIFFERENT family (a trend filter, a mean-reversion oscillator, a breakout channel). If lessons show setups consistently land just under Sharpe 1 but with a healthy profit factor (>1.3), you MAY lower minSharpe toward its 0.5 floor — the gate thresholds you pick are clamped to hard floors (minSharpe>=0.5, minProfitFactor>=1.2, maxDrawdownPct<=25, minTradeCount>=5), so propose within those. Current defaults: interval ${config.strategy.evalInterval}, candleLimit ${config.strategy.evalCandleLimit}, gate minSharpe ${activePolicy.minSharpe} / minProfitFactor ${activePolicy.minProfitFactor} / maxDrawdownPct ${activePolicy.maxDrawdownPct} / minTradeCount ${activePolicy.minTradeCount}.

Respond in JSON with exactly these fields: interval (one of "1h","2h","4h","6h","12h","1d"), candleLimit (integer 1000-20000), minSharpe (number 0.5-3), minProfitFactor (number 1.2-3), maxDrawdownPct (number 5-25), minTradeCount (integer 5-300), signalEmphasis (string: concrete guidance for the generator — which indicator family/approach and structure to favor or avoid given the lessons), reasoning (string).

Lessons:
${lessonsBlock}`;

    let plan: any;
    try {
      const res = await this.llmService.generateStructuredCompletion(planPrompt, GenerationPlanSchema, {
        schemaName: 'plan_generation',
      });
      plan = res.data;
      await this.llmService.logCost(ticker.id, null, LlmPurpose.STRATEGY_GENERATION, {
        content: JSON.stringify(plan),
        inputTokens: res.inputTokens,
        outputTokens: res.outputTokens,
        costUsd: res.costUsd,
        model: res.model,
        provider: res.provider,
      });
    } catch (err: any) {
      this.logger.warn(`Generation planner failed, using defaults: ${err.message}`);
      return defaultPlan;
    }
    if (!plan) return defaultPlan;

    // Hard safety floors (user-chosen): the AI tunes the gate WITHIN these, never below them.
    const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
    const effectivePolicy = {
      minSharpe: clamp(Number(plan.minSharpe), 0.5, 3),
      minProfitFactor: clamp(Number(plan.minProfitFactor), 1.2, 3),
      maxDrawdownPct: clamp(Number(plan.maxDrawdownPct), 5, 25),
      minTradeCount: Math.round(clamp(Number(plan.minTradeCount), 5, 300)),
      maxParameterCount: activePolicy.maxParameterCount, // fixed — never AI-controlled
    };

    this.logger.log(
      `Generation plan for ${ticker.symbol}: interval ${plan.interval}, candleLimit ${plan.candleLimit}, ` +
        `gate proposed sharpe/PF/DD/trades ${plan.minSharpe}/${plan.minProfitFactor}/${plan.maxDrawdownPct}/${plan.minTradeCount} ` +
        `-> clamped ${effectivePolicy.minSharpe}/${effectivePolicy.minProfitFactor}/${effectivePolicy.maxDrawdownPct}/${effectivePolicy.minTradeCount}. ` +
        `Emphasis: ${plan.signalEmphasis}`,
    );

    return {
      interval: plan.interval,
      candleLimit: plan.candleLimit,
      policy: effectivePolicy,
      signalEmphasis: plan.signalEmphasis,
      reasoning: plan.reasoning,
    };
  }

  /**
   * Adaptive loop, memory side: before a new generation, distill how the CURRENT live strategy is
   * actually performing (real-data replay + live closed positions + backtest divergence) into a
   * 'live_insight' lesson the planner reads. No-op when nothing is live yet.
   */
  private async recordLiveInsightForCurrentStrategy(ticker: Ticker): Promise<void> {
    const liveStrategy = await this.strategyRepo.findOne({
      where: { tickerId: ticker.id, status: StrategyStatus.LIVE },
    });
    if (!liveStrategy) return;

    const performance = await this.performanceService.getPerformance(liveStrategy.id);
    const closed = await this.positionRepo.find({
      where: { tickerId: ticker.id, strategyId: liveStrategy.id, status: PositionStatus.CLOSED },
    });
    const netPnlUsd = closed.reduce((s, p) => s + Number(p.realizedPl || 0), 0);
    const divergence = await this.divergenceRepo.findOne({
      where: { strategyId: liveStrategy.id },
      order: { measuredAt: 'DESC' },
    });

    await this.aiLessonsService.recordLiveInsightLesson({
      strategy: liveStrategy,
      ticker,
      performance,
      closedPositions: { count: closed.length, netPnlUsd },
      divergence,
    });
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
    // Phase 2 hybrid AI overlay (opt-in): only upgrades a rules HOLD to an early SELL on a winning
    // position; it can never turn a rules SELL into HOLD or open a position.
    const finalSignal =
      config.hybridExitAi && signal === 'HOLD'
        ? await this.applyHybridExitOverlay(tickerId, liveStrategy, currentCandle)
        : signal;
    return { signal: finalSignal, strategyId: liveStrategy.id };
  }

  /**
   * Phase 2 hybrid overlay: on a rules HOLD, let the AI book a WINNING open position early if the
   * up-move looks exhausted. Tightens only — returns 'SELL' or 'HOLD', never 'BUY'. Losers are left
   * to the deterministic stop/floor, so the AI budget is spent only protecting real gains. Mirrors
   * the worker's apply_hybrid_exit_overlay.
   */
  private async applyHybridExitOverlay(
    tickerId: string,
    liveStrategy: Strategy,
    currentCandle: { close: number },
  ): Promise<'SELL' | 'HOLD'> {
    const openPosition = await this.positionRepo.findOne({
      where: { tickerId, strategyId: liveStrategy.id, status: PositionStatus.OPEN },
    });
    if (!openPosition) return 'HOLD'; // nothing to protect
    if (Number(openPosition.unrealizedPl) <= 0) return 'HOLD'; // stop/floor owns losers; save the LLM call

    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    if (!ticker) return 'HOLD';
    const recentCandles = await this.ohlcvRepo.find({ where: { tickerId }, order: { timestamp: 'DESC' }, take: 20 });
    const closes = recentCandles.map((c) => Number(c.close)).reverse();
    const priceChangePct = closes.length >= 2 ? ((closes[closes.length - 1] - closes[0]) / closes[0]) * 100 : 0;
    const lessons = await this.aiLessonsService.retrieveRelevantLessons(
      tickerId,
      this.inferStrategyTypeTag(liveStrategy.parametersJson),
    );
    const lessonsBlock = this.aiLessonsService.formatLessonsForPrompt(lessons);
    const entry = Number(openPosition.entryPrice);
    const retPct = entry ? ((currentCandle.close - entry) / entry) * 100 : 0;

    const prompt = `Open-position exit check for ${ticker.symbol} (Interval: ${ticker.interval}).
You are LONG from ${entry}; latest price ${Number(currentCandle.close)}, unrealized ${retPct.toFixed(2)}% (${Number(openPosition.unrealizedPl)}).
Price change over the last ${closes.length} candles: ${priceChangePct.toFixed(2)}%.
The deterministic rules currently say HOLD (no stop / target / structure-break exit has fired). Your ONLY job: judge whether the up-move looks EXHAUSTED or about to reverse, so we should EXIT now and bank the profit, or HOLD to keep riding the trend for more.

Relevant lessons from past strategies on this ticker/strategy type:
${lessonsBlock}

Respond in JSON with EXIT or HOLD.`;

    const llmRes = await this.llmService.generateStructuredCompletion(prompt, ExitTightenDecisionSchema, {
      schemaName: 'propose_exit_tighten',
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
    return llmRes.data.action === 'EXIT' ? 'SELL' : 'HOLD';
  }

  /**
   * Mode A (spec 2.6) — deterministic, zero-LLM-call rule-tree execution via the SAME DSL
   * interpreter the backtest uses, so what trades live is exactly what was promoted. A legacy
   * indicatorConfig blob is auto-translated to a rule tree; any promoted rule tree runs directly.
   * (Previously this was hardcoded to EMA crossover.)
   */
  private async evaluateRulesSignal(tickerId: string, liveStrategy: Strategy): Promise<'BUY' | 'SELL' | 'HOLD'> {
    const ir = resolveIR(liveStrategy.parametersJson);
    if (!ir) {
      this.logger.warn(`Live strategy ${liveStrategy.id} has invalid params (not a valid rule tree); holding.`);
      return 'HOLD';
    }
    if (ir.direction === 'short') {
      // Short strategies are fully backtestable/promotable, but live short EXECUTION needs the
      // futures order path (Phase 4b). Until then the live loop stays flat rather than placing a
      // wrong-direction long order.
      this.logger.warn(`Live strategy ${liveStrategy.id} is a SHORT strategy; live short execution is not yet wired (Phase 4b). Holding.`);
      return 'HOLD';
    }

    // Evaluate on the SAME timeframe the strategy was backtested/promoted on
    // (STRATEGY_EVAL_INTERVAL, aggregated from the 1m base) — not raw 1m, which would fire a
    // higher-timeframe strategy on 1-minute noise. Pull enough lead-in bars for every indicator
    // in the tree to converge, so the signal at the last bar matches the backtest's.
    const warmup = warmupBars(ir);
    const need = Math.max(warmup * 5 + 20, 300);
    // Evaluate on the SAME timeframe this strategy was generated/backtested on (stored per
    // strategy), not the global config — otherwise what trades live wouldn't match what was promoted.
    const interval = liveStrategy.evalInterval || config.strategy.evalInterval;
    const candles = await this.marketDataService.getCandlesForInterval(tickerId, interval, need);
    if (candles.length < warmup + 2) return 'HOLD';

    const i = candles.length - 1; // getCandlesForInterval returns chronological (ASC) order

    const openPosition = await this.positionRepo.findOne({
      where: { tickerId, strategyId: liveStrategy.id, status: PositionStatus.OPEN },
    });

    if (!openPosition) {
      // Reconcile to the strategy's INTENDED exposure, not just a fresh entry edge on this bar:
      // if the strategy's own stateful replay says it should currently be holding (it entered on
      // an earlier bar and hasn't hit an exit), open the position to match it. This closes the
      // gap where activating a strategy that's already signalling long — or a backend restart
      // mid-trade — left the live system flat until the NEXT crossover, which reads as "I turned
      // it on and it never traded". In steady state (flat, waiting for a cross) this is identical
      // to the old edge check. `shouldEnter` is kept as the fast fresh-edge path.
      const intendedLong = shouldEnter(ir, candles as any, i) || intendedPositionState(ir, candles as any) === 'LONG';
      return intendedLong ? 'BUY' : 'HOLD';
    }

    // In a position: build the exit context (entry price + bars held + peak-since-entry) so the
    // risk block (SL/TP/trailing/max-hold) and the exit rule are evaluated correctly.
    const entryPrice = Number(openPosition.entryPrice);
    const entryTime = new Date(openPosition.openedAt).getTime();
    const candleTime = (c: any) => new Date(c.timestamp).getTime();
    let entryIndex = i;
    for (let k = candles.length - 1; k >= 0; k--) {
      if (candleTime(candles[k]) <= entryTime) { entryIndex = k; break; }
    }
    const barsHeld = Math.max(0, i - entryIndex);
    // Favorable extreme since entry (long path only here — shorts return above): highest HIGH.
    let extremePrice = entryPrice;
    for (let k = entryIndex; k <= i; k++) {
      const hi = Number((candles[k] as any).high ?? (candles[k] as any).close);
      if (hi > extremePrice) extremePrice = hi;
    }

    const reason = exitReason(ir, candles as any, i, { entryPrice, barsHeld, extremePrice });
    return reason ? 'SELL' : 'HOLD';
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
    const strategies = await this.strategyRepo.find({ where: { tickerId }, order: { version: 'DESC' } });
    if (!strategies.length) return [];
    const backtests = await this.backtestRepo.find({
      where: { strategyId: In(strategies.map((s) => s.id)) },
    });
    const byStrategy = new Map(backtests.map((b) => [b.strategyId, b]));
    return strategies.map((strategy) => ({ strategy, backtest: byStrategy.get(strategy.id) ?? null }));
  }

  /**
   * Manually promote a specific strategy version to LIVE for its ticker. Retires the
   * currently-live version (recording a lesson first, mirroring the auto-promotion path)
   * so exactly one strategy is ever live per ticker. Unlike generation this bypasses the
   * evaluation gate — it's a deliberate operator override.
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
      // Same orphan-prevention as the auto-promotion path: flatten open positions before the switch.
      await this.executionService
        .flattenOpenPositionsForTicker(tickerId, `manual activation: retiring v${previousLive.version} for v${strategy.version}`)
        .catch((err) => this.logger.warn(`Flatten-on-switch failed (non-fatal): ${err.message}`));

      const previousBacktest = await backtestFor(previousLive.id);
      const latestDivergence = await this.divergenceRepo.findOne({
        where: { strategyId: previousLive.id },
        order: { measuredAt: 'DESC' },
      });
      if (ticker) {
        await this.aiLessonsService
          .recordLesson({
            retiredStrategy: previousLive,
            retiredBacktest: previousBacktest,
            divergence: latestDivergence,
            ticker,
            reason: `manually superseded by v${strategy.version}`,
          })
          .catch((err) => this.logger.warn(`Lesson recording failed (non-fatal): ${err.message}`));
      }
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

  /**
   * Buy/sell intent markers for a ticker's currently-active (LIVE) strategy, replayed over
   * the same interval-rolled candles the chart renders so the markers line up bar-for-bar.
   * Empty when nothing is live.
   */
  async getSignalsForTicker(tickerId: string, interval = '15m', limit = 500) {
    const active = await this.strategyRepo.findOne({ where: { tickerId, status: StrategyStatus.LIVE } });
    if (!active) return { strategyId: null, strategyName: null, signals: [] };
    const candles = await this.marketDataService.getCandlesForInterval(tickerId, interval, limit);
    const signals = this.evaluatorService.generateSignals(candles, active.parametersJson);
    return {
      strategyId: active.id,
      strategyName: active.parametersJson?.strategyName || `Strategy v${active.version}`,
      signals,
    };
  }

  async getPolicies() {
    return this.policyRepo.find();
  }

  private inferStrategyTypeTag(params: Record<string, any>): string {
    return strategyTypeTag(params);
  }

  /**
   * Turn the LLM's generation output into a validated rule-tree IR: JSON.parse the `strategy`
   * string, assemble {strategyName, reasoning, entry, exit, risk}, and structurally validate it.
   * Returns null (a failed attempt) on parse error or any validation error.
   */
  private parseGeneratedStrategy(data: any): StrategyIR | null {
    // Prefer the natural nested shape (entry/exit/risk as top-level fields — what models emit).
    // Fall back to a `strategy` field (string or object) in case a model wraps it that way.
    let body: any = data;
    if (!data?.entry && !data?.exit && data?.strategy != null) {
      try {
        body = typeof data.strategy === 'string' ? JSON.parse(data.strategy) : data.strategy;
      } catch (err: any) {
        this.logger.warn(`Generated strategy "strategy" field did not parse: ${err.message}`);
        return null;
      }
    }
    const direction = data?.direction ?? body?.direction;
    const ir = {
      strategyName: data?.strategyName || 'Generated Strategy',
      reasoning: data?.reasoning || '',
      ...(direction ? { direction } : {}),
      entry: body?.entry,
      exit: body?.exit,
      risk: body?.risk,
    } as StrategyIR;
    const errs = validateIR(ir);
    if (errs.length) {
      this.logger.warn(`Generated strategy failed validation: ${errs.slice(0, 4).join('; ')}`);
      return null;
    }
    return ir;
  }
}
