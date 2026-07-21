import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn } from 'typeorm';

export enum LlmPurpose {
  STRATEGY_GENERATION = 'strategy_generation',
  RE_EVALUATION = 're_evaluation',
  LIVE_DECISION = 'live_decision',
}

@Entity({ name: 'llm_cost_log', schema: 'Algo_Trading' })
export class LlmCostLog {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id', nullable: true })
  tickerId: string;

  @Column({ name: 'strategy_id', nullable: true })
  strategyId: string;

  @Column({ type: 'enum', enum: LlmPurpose })
  purpose: LlmPurpose;

  @Column({ name: 'llm_provider', default: 'direct_api' })
  llmProvider: string;

  @Column()
  model: string;

  @Column({ name: 'input_tokens', type: 'int' })
  inputTokens: number;

  @Column({ name: 'output_tokens', type: 'int' })
  outputTokens: number;

  @Column({ name: 'cost_usd', type: 'decimal', precision: 10, scale: 6 })
  costUsd: number;

  @CreateDateColumn({ name: 'called_at' })
  calledAt: Date;
}
