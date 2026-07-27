import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Strategy } from './strategy.entity';
import { LiveVsBacktestDivergence } from './live-vs-backtest-divergence.entity';
import { Ticker } from './ticker.entity';

export enum LessonOutcome {
  SUCCESS = 'success',
  FAILURE = 'failure',
}

// Spec Section 8.5 / 4.11 — distilled, generalized lessons from strategy retirement/
// replacement, consulted before future strategy generation so the AI doesn't repeat
// documented mistakes. Summarized text only, never raw trade logs (Section 4.11.1).
@Entity({ name: 'ai_lessons_learned', schema: 'Algo_Trading' })
export class AiLessonLearned {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  // Nullable: a lesson distilled from a FAILED generation cycle has no persisted strategy to
  // point at (gate-before-save means failed attempts are never written to `strategies`).
  @Column({ name: 'source_strategy_id', nullable: true })
  sourceStrategyId: string | null;

  @ManyToOne(() => Strategy, { onDelete: 'CASCADE', nullable: true })
  @JoinColumn({ name: 'source_strategy_id' })
  sourceStrategy: Strategy;

  @Column({ name: 'source_divergence_id', nullable: true })
  sourceDivergenceId: string;

  @ManyToOne(() => LiveVsBacktestDivergence, { onDelete: 'SET NULL', nullable: true })
  @JoinColumn({ name: 'source_divergence_id' })
  sourceDivergence: LiveVsBacktestDivergence;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column({ name: 'market_type', nullable: true })
  marketType: string;

  @Column({ name: 'strategy_type', nullable: true })
  strategyType: string;

  @Column({ name: 'regime_tags', type: 'jsonb', nullable: true })
  regimeTags: string[];

  @Column({ type: 'enum', enum: LessonOutcome })
  outcome: LessonOutcome;

  @Column({ name: 'summary_text', type: 'text' })
  summaryText: string;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;
}
