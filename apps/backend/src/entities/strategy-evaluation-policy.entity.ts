import { Entity, PrimaryGeneratedColumn, Column } from 'typeorm';

@Entity({ name: 'strategy_evaluation_policy', schema: 'Algo_Trading' })
export class StrategyEvaluationPolicy {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ default: 'Default Strict Policy' })
  name: string;

  @Column({ name: 'min_sharpe', type: 'decimal', precision: 5, scale: 2, default: 1.0 })
  minSharpe: number;

  @Column({ name: 'max_drawdown_pct', type: 'decimal', precision: 5, scale: 2, default: 20.0 })
  maxDrawdownPct: number;

  @Column({ name: 'min_profit_factor', type: 'decimal', precision: 5, scale: 2, default: 1.3 })
  minProfitFactor: number;

  @Column({ name: 'min_trade_count', type: 'int', default: 100 })
  minTradeCount: number;

  @Column({ name: 'max_parameter_count', type: 'int', default: 5 })
  maxParameterCount: number;

  @Column({ name: 'is_active', default: true })
  isActive: boolean;
}
