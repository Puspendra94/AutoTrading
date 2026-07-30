import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Ticker } from './ticker.entity';

// One row per evaluated bar — including the free ones where the gate declined and no LLM call
// was made. Logging the skips is what makes an over-tight gate visible: without them the system
// just never trades and nothing says why.
export enum PatternDecisionOutcome {
  SKIPPED_NO_TRIGGER = 'skipped_no_trigger', // gate declined; no LLM call, no cost
  SKIPPED_BY_MODEL = 'skipped_by_model', // model was asked and chose not to trade
  REJECTED = 'rejected', // model proposed a trade the guardrails refused
  EXECUTED = 'executed',
  EXIT_EXECUTED = 'exit_executed',
  EXIT_HELD = 'exit_held',
  ERROR = 'error',
}

@Entity({ name: 'pattern_decisions', schema: 'Algo_Trading' })
export class PatternDecision {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column()
  interval: string;

  @Column({ name: 'bar_time', type: 'timestamptz' })
  barTime: Date;

  @Column()
  outcome: string;

  @Column({ type: 'text', default: '' })
  reason: string;

  // Why the gate did or did not spend a call, and which triggers drove it.
  @Column({ type: 'jsonb', nullable: true })
  gate: Record<string, unknown> | null;

  // The exact feature state the decision was made on, so it can be replayed against a changed
  // prompt without recomputing indicators or paying for inference again.
  @Column({ name: 'state_pack', type: 'jsonb', nullable: true })
  statePack: Record<string, unknown> | null;

  // The model's raw proposal, stored UNMODIFIED even when rejected — repairing it would put a
  // trade nobody proposed into the audit trail.
  @Column({ name: 'llm_decision', type: 'jsonb', nullable: true })
  llmDecision: Record<string, unknown> | null;

  @Column({ type: 'jsonb', nullable: true })
  validation: Record<string, unknown> | null;

  @Column({ type: 'jsonb', nullable: true })
  sizing: Record<string, unknown> | null;

  @Column({ name: 'position_id', type: 'uuid', nullable: true })
  positionId: string | null;

  @Column({ name: 'stop_price', type: 'decimal', precision: 18, scale: 8, nullable: true })
  stopPrice: number | null;

  @Column({ name: 'take_profit', type: 'decimal', precision: 18, scale: 8, nullable: true })
  takeProfit: number | null;

  @Column({ type: 'varchar', nullable: true })
  model: string | null;

  @Column({ name: 'cost_usd', type: 'decimal', precision: 12, scale: 6, default: 0 })
  costUsd: number;

  // Provider-reported usage. cost_usd is 0 for any model missing from LlmService.PRICING
  // (DeepSeek is), so these are the usage signal that always holds.
  @Column({ name: 'input_tokens', type: 'int', default: 0 })
  inputTokens: number;

  @Column({ name: 'output_tokens', type: 'int', default: 0 })
  outputTokens: number;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
