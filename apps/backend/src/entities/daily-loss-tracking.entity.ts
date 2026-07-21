import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

@Entity({ name: 'daily_loss_tracking', schema: 'Algo_Trading' })
export class DailyLossTracking {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'tracking_date', type: 'date' })
  trackingDate: string; // YYYY-MM-DD

  @Column({ name: 'capital_under_management_base', type: 'decimal', precision: 18, scale: 8 })
  capitalUnderManagementBase: number;

  @Column({ name: 'realized_pl', type: 'decimal', precision: 18, scale: 8, default: 0 })
  realizedPl: number;

  @Column({ name: 'unrealized_pl', type: 'decimal', precision: 18, scale: 8, default: 0 })
  unrealizedPl: number;

  @Column({ name: 'limit_breached', default: false })
  limitBreached: boolean;

  @Column({ name: 'breached_at', type: 'timestamptz', nullable: true })
  breachedAt: Date;
}
