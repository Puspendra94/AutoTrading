import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

@Entity({ name: 'provider_balance_snapshots', schema: 'Algo_Trading' })
export class ProviderBalanceSnapshot {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'tradable_balance', type: 'decimal', precision: 18, scale: 8 })
  tradableBalance: number;

  @Column({ default: 'USDT' })
  currency: string;

  @CreateDateColumn({ name: 'synced_at' })
  syncedAt: Date;
}
