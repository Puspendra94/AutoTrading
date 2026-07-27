import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Ticker } from './ticker.entity';

export enum StrategyStatus {
  DRAFT = 'draft',
  BACKTESTED = 'backtested',
  EVALUATED = 'evaluated',
  LIVE = 'live',
  RETIRED = 'retired',
}

export enum ExecutionMode {
  MODE_A_RULES = 'mode_a_rules',
  MODE_B_AI_LIVE = 'mode_b_ai_live',
}

@Entity({ name: 'strategies', schema: 'Algo_Trading' })
export class Strategy {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column({ type: 'int', default: 1 })
  version: number;

  @Column({ type: 'enum', enum: StrategyStatus, default: StrategyStatus.DRAFT })
  status: StrategyStatus;

  @Column({ name: 'execution_mode', type: 'enum', enum: ExecutionMode, default: ExecutionMode.MODE_A_RULES })
  executionMode: ExecutionMode;

  @Column({ name: 'parameters_json', type: 'jsonb' })
  parametersJson: Record<string, any>;

  // The timeframe this strategy was generated/backtested on AND is evaluated live on. Stored per
  // strategy (not just the global config default) so what trades live is exactly what was
  // promoted, and so the generation timeframe can be chosen per run. Null -> fall back to config.
  @Column({ name: 'eval_interval', type: 'varchar', nullable: true })
  evalInterval: string | null;

  @Column({ name: 'generated_by', default: 'claude-3-5-sonnet' })
  generatedBy: string;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;
}
