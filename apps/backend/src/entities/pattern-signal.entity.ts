import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Ticker } from './ticker.entity';

// A trigger detected by the pattern brain's feature engine — a level break, a completed chart
// pattern, a regime flip. Written once per (ticker, interval, bar, kind); the read path buckets
// bar_time onto whatever timeframe the chart is showing.
//
// Note this is NOT the strategy brain's `strategy_signals`. See migration 1700000017000 for why
// the two are kept apart (a nullable strategy_id would defeat that table's unique constraint).
@Entity({ name: 'pattern_signals', schema: 'Algo_Trading' })
export class PatternSignal {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  // The timeframe the feature engine EVALUATED on (not the chart's display interval).
  @Column()
  interval: string;

  @Column({ name: 'bar_time', type: 'timestamptz' })
  barTime: Date;

  // Trigger name: resistance_break, support_break, pullback_to_support, range_breakout_up,
  // pattern_complete, regime_flip, …
  @Column()
  kind: string;

  // The side this trigger would imply — 'long' | 'short'. Not a decision, just a direction.
  @Column()
  side: string;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  price: number;

  @Column({ default: '' })
  detail: string;

  // The full feature state at that bar, so a decision can be reviewed or replayed later against
  // exactly the numbers that produced it — without recomputing or re-paying for anything.
  @Column({ name: 'state_pack', type: 'jsonb', nullable: true })
  statePack: Record<string, unknown> | null;

  @CreateDateColumn({ name: 'created_at', type: 'timestamptz' })
  createdAt: Date;
}
