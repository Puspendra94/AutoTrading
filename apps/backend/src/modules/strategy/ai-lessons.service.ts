import { Injectable, Logger } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { AiLessonLearned, LessonOutcome } from '../../entities/ai-lesson-learned.entity';
import { Strategy } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { LiveVsBacktestDivergence } from '../../entities/live-vs-backtest-divergence.entity';
import { Ticker } from '../../entities/ticker.entity';
import { LlmService } from '../llm/llm.service';
import { LlmPurpose } from '../../entities/llm-cost-log.entity';
import { strategyTypeTag, describeStrategy } from './dsl/strategy-ir';

/**
 * Spec Section 4.11 — AI Memory & Continuous Learning. Retrieval is deliberately
 * tag-based (ticker + strategy-type), per the spec's explicit "start simple, iterate
 * later" guidance — only reach for embedding/similarity search if this proves too coarse.
 */
@Injectable()
export class AiLessonsService {
  private readonly logger = new Logger(AiLessonsService.name);

  constructor(
    @InjectRepository(AiLessonLearned)
    private readonly lessonRepo: Repository<AiLessonLearned>,
    private readonly llmService: LlmService,
  ) {}

  async retrieveRelevantLessons(tickerId: string, strategyType?: string, limit = 5): Promise<AiLessonLearned[]> {
    const qb = this.lessonRepo
      .createQueryBuilder('lesson')
      .where('lesson.tickerId = :tickerId', { tickerId })
      .orderBy('lesson.createdAt', 'DESC')
      .take(limit);

    const tickerMatches = await qb.getMany();
    if (tickerMatches.length >= limit || !strategyType) return tickerMatches;

    // Backfill with lessons from the same strategy type on other tickers if the
    // ticker-specific pool is thin — still a simple tag match, no embeddings.
    const remaining = limit - tickerMatches.length;
    const seenIds = new Set(tickerMatches.map((l) => l.id));
    const broader = await this.lessonRepo
      .createQueryBuilder('lesson')
      .where('lesson.strategyType = :strategyType', { strategyType })
      .andWhere('lesson.tickerId != :tickerId', { tickerId })
      .orderBy('lesson.createdAt', 'DESC')
      .take(remaining + seenIds.size)
      .getMany();

    return [...tickerMatches, ...broader.filter((l) => !seenIds.has(l.id))].slice(0, limit);
  }

  /** Condenses relevant lessons into a short block for inclusion in a generation prompt —
   * summarized text only, never raw trade logs (spec 4.11.1/2.7 token-efficiency rule). */
  formatLessonsForPrompt(lessons: AiLessonLearned[]): string {
    if (lessons.length === 0) return 'No prior lessons recorded for this ticker/strategy type yet.';
    return lessons
      .map((l, i) => `${i + 1}. [${l.outcome.toUpperCase()}] ${l.summaryText}`)
      .join('\n');
  }

  async recordLesson(params: {
    retiredStrategy: Strategy;
    retiredBacktest?: BacktestResult | null;
    divergence?: LiveVsBacktestDivergence | null;
    ticker: Ticker;
    reason: string;
  }): Promise<AiLessonLearned> {
    const { retiredStrategy, retiredBacktest, divergence, ticker, reason } = params;

    const outcome = divergence?.flagged || (retiredBacktest && !retiredBacktest.passedEvaluationGate)
      ? LessonOutcome.FAILURE
      : LessonOutcome.SUCCESS;

    const summaryPrompt = `A trading strategy was just retired. Distill this into ONE short, generalized,
reusable lesson (2-3 sentences) for future strategy generation on this or similar tickers — focus on the
pattern/cause, not a trade-by-trade recap. Ticker: ${ticker.symbol}. Strategy parameters: ${JSON.stringify(
      retiredStrategy.parametersJson,
    )}. Backtest metrics: ${JSON.stringify({
      sharpe: retiredBacktest?.sharpe,
      profitFactor: retiredBacktest?.profitFactor,
      maxDrawdown: retiredBacktest?.maxDrawdown,
    })}. Retirement reason: ${reason}. Live-vs-backtest divergence: ${
      divergence ? `${divergence.divergencePct}% on the tracked metric` : 'n/a'
    }. Respond with plain text only, no JSON.`;

    // Deterministic fallback — used whenever the LLM summary is unavailable OR empty (reasoning
    // models can return no text after spending the whole budget on hidden reasoning).
    const fallbackText = `Strategy v${retiredStrategy.version} for ${ticker.symbol} was retired (${reason}). Backtest Sharpe ${retiredBacktest?.sharpe ?? 'n/a'}, profit factor ${retiredBacktest?.profitFactor ?? 'n/a'}.`;
    let summaryText = '';
    try {
      const llmRes = await this.llmService.generateCompletion(summaryPrompt, { maxTokens: 800 });
      summaryText = (llmRes.content || '').trim();
      await this.llmService.logCost(ticker.id, retiredStrategy.id, LlmPurpose.RE_EVALUATION, llmRes);
    } catch (err) {
      this.logger.warn(`Lesson summarization LLM call failed, using a structured fallback: ${err.message}`);
    }
    if (!summaryText) summaryText = fallbackText;

    const lesson = this.lessonRepo.create({
      sourceStrategyId: retiredStrategy.id,
      sourceDivergenceId: divergence?.id,
      tickerId: ticker.id,
      marketType: ticker.marketType?.name,
      strategyType: this.inferStrategyType(retiredStrategy.parametersJson),
      regimeTags: [],
      outcome,
      summaryText,
    });
    return this.lessonRepo.save(lesson);
  }

  /**
   * Spec 4.11 continuous learning, applied to the FAILURE path: when a whole generation cycle
   * fails the evaluation gate (nothing promoted), distill *why* — the parameters tried and the
   * gate conditions they missed — into a reusable FAILURE lesson. Retrieval feeds it straight
   * back into the next generation prompt, so the model steers away from the same dead ends
   * instead of rediscovering them. No source strategy exists (nothing was persisted), so
   * sourceStrategyId is null.
   */
  async recordFailureLesson(params: {
    ticker: Ticker;
    attemptedParams: any;
    evaluation: any;
    failingConditions: string[];
    reason: string;
  }): Promise<AiLessonLearned> {
    const { ticker, attemptedParams, evaluation, failingConditions, reason } = params;
    const conditions = failingConditions.join('; ') || 'unknown';
    const metrics = JSON.stringify({
      sharpe: evaluation?.sharpe,
      profitFactor: evaluation?.profitFactor,
      maxDrawdown: evaluation?.maxDrawdown,
      tradeCount: evaluation?.tradeCount,
    });

    const summaryPrompt = `A strategy generation attempt just failed the evaluation gate and was discarded (never traded). Distill this into ONE short, generalized, reusable lesson (2-3 sentences) for the next strategy generation on this or similar tickers — focus on what to change to clear the gate and improve profitability, not a trade-by-trade recap. Ticker: ${ticker.symbol}. Attempted parameters: ${JSON.stringify(
      attemptedParams,
    )}. Out-of-sample backtest metrics: ${metrics}. Failing gate conditions: ${conditions}. Trigger: ${reason}. Respond with plain text only, no JSON.`;

    // Deterministic fallback with the concrete params + failing conditions — used whenever the
    // LLM summary is unavailable OR empty. Reasoning models (e.g. deepseek-v4-pro) can spend the
    // whole token budget on hidden reasoning and return no text, which would otherwise persist a
    // blank, useless lesson and starve the learning loop.
    const fallbackText = `A generated ${describeStrategy(attemptedParams)} for ${ticker.symbol} failed the gate (${conditions}); out-of-sample Sharpe ${evaluation?.sharpe ?? 'n/a'}, profit factor ${evaluation?.profitFactor ?? 'n/a'}.`;
    let summaryText = '';
    try {
      const llmRes = await this.llmService.generateCompletion(summaryPrompt, { maxTokens: 800 });
      summaryText = (llmRes.content || '').trim();
      await this.llmService.logCost(ticker.id, null, LlmPurpose.RE_EVALUATION, llmRes);
    } catch (err) {
      this.logger.warn(`Failure-lesson summarization LLM call failed, using a structured fallback: ${err.message}`);
    }
    if (!summaryText) summaryText = fallbackText;

    const lesson = this.lessonRepo.create({
      sourceStrategyId: null,
      sourceDivergenceId: null,
      tickerId: ticker.id,
      marketType: ticker.marketType?.name,
      strategyType: this.inferStrategyType(attemptedParams),
      regimeTags: [],
      outcome: LessonOutcome.FAILURE,
      summaryText,
    });
    return this.lessonRepo.save(lesson);
  }

  /**
   * Live-performance memory (adaptive loop): distill how the ACTIVE strategy is really doing —
   * from its real-data replay + live closed positions + backtest divergence — into an insight the
   * meta-planner reads before the next generation. This is the "how it's working / what to
   * improve" memory, separate from failed-generation lessons. Tagged 'live_insight'.
   */
  async recordLiveInsightLesson(params: {
    strategy: Strategy;
    ticker: Ticker;
    performance: any; // StrategyPerformance row (real-data replay), may be null
    closedPositions: { count: number; netPnlUsd: number };
    divergence?: LiveVsBacktestDivergence | null;
  }): Promise<AiLessonLearned> {
    const { strategy, ticker, performance, closedPositions, divergence } = params;
    const perf = performance || {};
    const outcome = Number(perf.totalReturnPct ?? 0) > 0 ? LessonOutcome.SUCCESS : LessonOutcome.FAILURE;

    const summaryPrompt = `A live trading strategy is being reviewed before the next generation cycle. In 2-3 sentences, summarize how it is actually performing and what would make the NEXT strategy better — focus on actionable direction (timeframe, EMA periods, the trend filter, stop/target sizing, trade frequency), not a trade-by-trade recap. Ticker: ${ticker.symbol}. Strategy parameters: ${JSON.stringify(
      strategy.parametersJson,
    )}. Real-data backtest: total return ${perf.totalReturnPct ?? 'n/a'}%, Sharpe ${perf.sharpe ?? 'n/a'}, win rate ${perf.winRate ?? 'n/a'}%, max drawdown ${perf.maxDrawdownPct ?? 'n/a'}%, trades ${perf.tradeCount ?? 'n/a'}. Live closed positions: ${closedPositions.count} (net P/L ${closedPositions.netPnlUsd.toFixed(2)} USD). Live-vs-backtest divergence: ${
      divergence ? `${divergence.divergencePct}%` : 'n/a'
    }. Respond with plain text only, no JSON.`;

    const fallbackText = `Live review of ${ticker.symbol} ${describeStrategy(strategy.parametersJson)}: real-data total return ${perf.totalReturnPct ?? 'n/a'}%, Sharpe ${perf.sharpe ?? 'n/a'}, ${perf.tradeCount ?? 'n/a'} trades, ${closedPositions.count} live trades (net ${closedPositions.netPnlUsd.toFixed(2)}).`;
    let summaryText = '';
    try {
      const llmRes = await this.llmService.generateCompletion(summaryPrompt, { maxTokens: 800 });
      summaryText = (llmRes.content || '').trim();
      await this.llmService.logCost(ticker.id, strategy.id, LlmPurpose.RE_EVALUATION, llmRes);
    } catch (err) {
      this.logger.warn(`Live-insight summarization failed, using a structured fallback: ${err.message}`);
    }
    if (!summaryText) summaryText = fallbackText;

    const lesson = this.lessonRepo.create({
      sourceStrategyId: strategy.id,
      sourceDivergenceId: divergence?.id,
      tickerId: ticker.id,
      marketType: ticker.marketType?.name,
      strategyType: this.inferStrategyType(strategy.parametersJson),
      regimeTags: ['live_insight'],
      outcome,
      summaryText,
    });
    return this.lessonRepo.save(lesson);
  }

  private inferStrategyType(params: Record<string, any>): string {
    // Coarse tag (distinct indicator kinds in the rule tree) — enough to group similar
    // strategies for retrieval. Legacy params are auto-translated first. See strategyTypeTag.
    return strategyTypeTag(params);
  }
}
