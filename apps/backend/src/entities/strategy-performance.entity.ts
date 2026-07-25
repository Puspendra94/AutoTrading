import { Entity, PrimaryGeneratedColumn, Column, UpdateDateColumn, OneToOne, JoinColumn } from 'typeorm';
import { Strategy } from './strategy.entity';

export enum PerformanceStatus {
  PENDING = 'pending',
  READY = 'ready',
  FAILED = 'failed',
}

/**
 * Full-history "real data" replay of a strategy: the buy/sell intents it produced over
 * every stored candle, the round-trip trades those intents formed, and the resulting
 * P/L and drawdown. Computed in the background (StrategyPerformanceService) and refreshed
 * on demand — one row per strategy.
 */
@Entity({ name: 'strategy_performance', schema: 'Algo_Trading' })
export class StrategyPerformance {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'strategy_id', unique: true })
  strategyId: string;

  @OneToOne(() => Strategy, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'strategy_id' })
  strategy: Strategy;

  @Column({ type: 'enum', enum: PerformanceStatus, default: PerformanceStatus.PENDING })
  status: PerformanceStatus;

  @Column({ name: 'data_from', type: 'timestamptz', nullable: true })
  dataFrom: Date;

  @Column({ name: 'data_to', type: 'timestamptz', nullable: true })
  dataTo: Date;

  @Column({ name: 'candle_count', type: 'bigint', default: 0 })
  candleCount: number;

  @Column({ name: 'intent_count', type: 'int', default: 0 })
  intentCount: number;

  @Column({ name: 'trade_count', type: 'int', default: 0 })
  tradeCount: number;

  @Column({ name: 'open_positions', type: 'int', default: 0 })
  openPositions: number;

  @Column({ name: 'win_rate', type: 'decimal', precision: 5, scale: 1, default: 0 })
  winRate: number;

  @Column({ name: 'total_return_pct', type: 'decimal', precision: 14, scale: 2, default: 0 })
  totalReturnPct: number;

  @Column({ name: 'total_pnl_usd', type: 'decimal', precision: 18, scale: 2, default: 0 })
  totalPnlUsd: number;

  @Column({ name: 'max_drawdown_pct', type: 'decimal', precision: 10, scale: 2, default: 0 })
  maxDrawdownPct: number;

  @Column({ type: 'decimal', precision: 10, scale: 2, default: 0 })
  sharpe: number;

  @Column({ name: 'avg_trade_return_pct', type: 'decimal', precision: 10, scale: 3, default: 0 })
  avgTradeReturnPct: number;

  @Column({ name: 'notional_usd', type: 'decimal', precision: 18, scale: 2, default: 10000 })
  notionalUsd: number;

  // Most-recent trades (bounded) with entry/exit/side/return/pnl/reason for a detail table.
  @Column({ name: 'trades_json', type: 'jsonb', nullable: true })
  tradesJson: Record<string, any>[];

  @Column({ name: 'error', type: 'text', nullable: true })
  error: string;

  @UpdateDateColumn({ name: 'computed_at' })
  computedAt: Date;
}
