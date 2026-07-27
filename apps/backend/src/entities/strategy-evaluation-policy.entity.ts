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

  // Required out-of-sample trades. Tuned to what's reachable on the eval interval (see
  // config.strategy.evalInterval) — the gate requires this directly, with no hidden override.
  @Column({ name: 'min_trade_count', type: 'int', default: 8 })
  minTradeCount: number;

  @Column({ name: 'max_parameter_count', type: 'int', default: 5 })
  maxParameterCount: number;

  // Optional whipsaw guard: minimum average out-of-sample hold (in eval-interval bars). NULL =
  // no constraint (default). Set it to reject over-trading strategies that churn in and out on
  // noise — the exact failure mode seen live (hundreds of tiny round-trips bleeding fees).
  @Column({ name: 'min_avg_hold_bars', type: 'decimal', precision: 6, scale: 2, nullable: true })
  minAvgHoldBars: number | null;

  @Column({ name: 'is_active', default: true })
  isActive: boolean;
}
