import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Ticker } from './ticker.entity';

export enum FlagType {
  GAP = 'gap',
  DUPLICATE = 'duplicate',
  SPIKE = 'spike',
  TZ_MISMATCH = 'tz_mismatch',
}

@Entity({ name: 'data_quality_flags', schema: 'Algo_Trading' })
export class DataQualityFlag {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column({ name: 'flag_type', type: 'enum', enum: FlagType })
  flagType: FlagType;

  @Column({ name: 'detail_json', type: 'jsonb', nullable: true })
  detailJson: Record<string, any>;

  @CreateDateColumn({ name: 'detected_at' })
  detectedAt: Date;

  @Column({ name: 'resolved_at', type: 'timestamptz', nullable: true })
  resolvedAt: Date;
}
