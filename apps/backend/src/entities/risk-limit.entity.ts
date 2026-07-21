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

  @Column({ name: 'reset_boundary', type: 'enum', enum: ResetBoundary, default: ResetBoundary.UTC_MIDNIGHT })
  resetBoundary: ResetBoundary;
}
