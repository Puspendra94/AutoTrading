import { Entity, PrimaryColumn, Column, ManyToOne, JoinColumn, Index } from 'typeorm';
import { Ticker } from './ticker.entity';

@Entity({ name: 'ohlcv_data', schema: 'Algo_Trading' })
@Index(['tickerId', 'timestamp'], { unique: true })
export class OhlcvData {
  @PrimaryColumn({ name: 'ticker_id', type: 'uuid' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @PrimaryColumn({ type: 'timestamptz' })
  timestamp: Date;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  open: number;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  high: number;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  low: number;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  close: number;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  volume: number;
}
