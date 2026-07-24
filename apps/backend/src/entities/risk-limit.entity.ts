import { Entity, PrimaryGeneratedColumn, Column, OneToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

export enum ResetBoundary {
  EXCHANGE_DAY = 'exchange_day',
  UTC_MIDNIGHT = 'utc_midnight',
}

@Entity({ name: 'risk_limits', schema: 'Algo_Trading' })
export class RiskLimit {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id', unique: true })
  providerId: string;

  @OneToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'daily_loss_limit_pct', type: 'decimal', precision: 5, scale: 2, default: 2.0 })
  dailyLossLimitPct: number;

  @Column({ name: 'max_concurrent_positions_per_ticker', type: 'int', default: 1 })
  maxConcurrentPositionsPerTicker: number;

  @Column({ name: 'probation_size_pct', type: 'decimal', precision: 5, scale: 2, default: 25.0 })
  probationSizePct: number;

  // Number of trades a newly-promoted strategy spends at probationSizePct before
  // scaling to full allocation (spec 2.5/12) — was previously only a read-nowhere
  // DEFAULT_PROBATION_TRADES_COUNT env var; now a real, per-provider policy value.
  @Column({ name: 'probation_trades_count', type: 'int', default: 10 })
  probationTradesCount: number;

  @Column({ name: 'reset_boundary', type: 'enum', enum: ResetBoundary, default: ResetBoundary.UTC_MIDNIGHT })
  resetBoundary: ResetBoundary;

  // Hard, provider-level exit guardrails enforced by MarketStreamService on every tick,
  // independent of any strategy's own stop-loss. Null = no hard cap. Configurable from the
  // Profile page.
  @Column({ name: 'hard_stop_loss_pct', type: 'decimal', precision: 5, scale: 2, nullable: true })
  hardStopLossPct: number | null;

  @Column({ name: 'hard_take_profit_pct', type: 'decimal', precision: 5, scale: 2, nullable: true })
  hardTakeProfitPct: number | null;
}
