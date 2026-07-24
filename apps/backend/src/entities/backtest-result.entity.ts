import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, OneToOne, JoinColumn } from 'typeorm';
import { Strategy } from './strategy.entity';

@Entity({ name: 'backtest_results', schema: 'Algo_Trading' })
export class BacktestResult {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'strategy_id', unique: true })
  strategyId: string;

  @OneToOne(() => Strategy, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'strategy_id' })
  strategy: Strategy;

  @Column({ type: 'decimal', precision: 10, scale: 4 })
  sharpe: number;

  @Column({ type: 'decimal', precision: 10, scale: 4 })
  sortino: number;

  @Column({ type: 'decimal', precision: 10, scale: 4 })
  calmar: number;

  @Column({ name: 'max_drawdown', type: 'decimal', precision: 10, scale: 4 })
  maxDrawdown: number;

  @Column({ name: 'drawdown_duration', type: 'int' })
  drawdownDuration: number; // in hours or days

  @Column({ name: 'profit_factor', type: 'decimal', precision: 10, scale: 4 })
  profitFactor: number;

  @Column({ name: 'trade_count', type: 'int' })
  tradeCount: number;

  @Column({ name: 'monte_carlo_summary_json', type: 'jsonb', nullable: true })
  monteCarloSummaryJson: Record<string, any>;

  @Column({ name: 'regime_breakdown_json', type: 'jsonb', nullable: true })
  regimeBreakdownJson: Record<string, any>;

  @Column({ name: 'parameter_count', type: 'int', default: 0 })
  parameterCount: number;

  @Column({ name: 'passed_evaluation_gate', default: false })
  passedEvaluationGate: boolean;

  @CreateDateColumn({ name: 'evaluated_at' })
  evaluatedAt: Date;
}
