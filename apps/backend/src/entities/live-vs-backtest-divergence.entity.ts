import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Strategy } from './strategy.entity';

@Entity({ name: 'live_vs_backtest_divergence', schema: 'Algo_Trading' })
export class LiveVsBacktestDivergence {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'strategy_id' })
  strategyId: string;

  @ManyToOne(() => Strategy, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'strategy_id' })
  strategy: Strategy;

  @CreateDateColumn({ name: 'measured_at' })
  measuredAt: Date;

  @Column({ name: 'backtest_expected_metric', type: 'decimal', precision: 10, scale: 4 })
  backtestExpectedMetric: number;

  @Column({ name: 'live_actual_metric', type: 'decimal', precision: 10, scale: 4 })
  liveActualMetric: number;

  @Column({ name: 'divergence_pct', type: 'decimal', precision: 5, scale: 2 })
  divergencePct: number;

  @Column({ default: false })
  flagged: boolean;
}
