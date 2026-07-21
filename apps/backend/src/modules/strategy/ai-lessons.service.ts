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

    let summaryText: string;
    try {
      const llmRes = await this.llmService.generateCompletion(summaryPrompt, { maxTokens: 200 });
      summaryText = llmRes.content.trim();
      await this.llmService.logCost(ticker.id, retiredStrategy.id, LlmPurpose.RE_EVALUATION, llmRes);
    } catch (err) {
      this.logger.warn(`Lesson summarization LLM call failed, using a structured fallback: ${err.message}`);
      summaryText = `Strategy v${retiredStrategy.version} for ${ticker.symbol} was retired (${reason}). Backtest Sharpe ${retiredBacktest?.sharpe ?? 'n/a'}, profit factor ${retiredBacktest?.profitFactor ?? 'n/a'}.`;
    }

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

  private inferStrategyType(params: Record<string, any>): string {
    // Coarse tag from the indicator config the LLM proposed — enough to group similar
    // strategies for retrieval without a taxonomy the spec doesn't ask for.
    if (params?.indicatorConfig?.rsiPeriod) return 'ema_rsi_trend';
    if (params?.indicatorConfig?.emaFastPeriod) return 'ema_crossover';
    return 'unclassified';
  }
}
