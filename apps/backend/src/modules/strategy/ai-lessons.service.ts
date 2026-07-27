import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { AiLessonLearned } from '../../entities/ai-lesson-learned.entity';

/**
 * Spec Section 4.11 — AI Memory & Continuous Learning (read side). Lesson RECORDING now happens in
 * the worker (which owns the strategy engine); the backend only RETRIEVES lessons to serve the
 * frontend's /lessons endpoint. Retrieval is tag-based (ticker + strategy-type), per the spec's
 * "start simple" guidance.
 */
@Injectable()
export class AiLessonsService {
  constructor(
    @InjectRepository(AiLessonLearned)
    private readonly lessonRepo: Repository<AiLessonLearned>,
  ) {}

  async retrieveRelevantLessons(tickerId: string, strategyType?: string, limit = 5): Promise<AiLessonLearned[]> {
    const tickerMatches = await this.lessonRepo
      .createQueryBuilder('lesson')
      .where('lesson.tickerId = :tickerId', { tickerId })
      .orderBy('lesson.createdAt', 'DESC')
      .take(limit)
      .getMany();
    if (tickerMatches.length >= limit || !strategyType) return tickerMatches;

    // Backfill with lessons of the same strategy type on other tickers if the ticker-specific pool
    // is thin — still a simple tag match, no embeddings.
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
}
