import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';
import { Ticker } from './ticker.entity';

@Entity({ name: 'allocation_snapshots', schema: 'Algo_Trading' })
export class AllocationSnapshot {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column({ name: 'allocated_capital', type: 'decimal', precision: 18, scale: 8 })
  allocatedCapital: number;

  @Column({ name: 'allocation_pct', type: 'decimal', precision: 5, scale: 2 })
  allocationPct: number;

  @CreateDateColumn({ name: 'computed_at' })
  computedAt: Date;
}
