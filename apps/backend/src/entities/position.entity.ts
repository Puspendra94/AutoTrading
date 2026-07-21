import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn, OneToMany } from 'typeorm';
import { Ticker } from './ticker.entity';
import { Strategy } from './strategy.entity';
import { Order } from './order.entity';

export enum PositionStatus {
  OPEN = 'open',
  CLOSED = 'closed',
}

export enum PositionSide {
  LONG = 'long',
  SHORT = 'short',
}

@Entity({ name: 'positions', schema: 'Algo_Trading' })
export class Position {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'ticker_id' })
  tickerId: string;

  @ManyToOne(() => Ticker, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'ticker_id' })
  ticker: Ticker;

  @Column({ name: 'strategy_id', nullable: true })
  strategyId: string;

  @ManyToOne(() => Strategy, { onDelete: 'SET NULL', nullable: true })
  @JoinColumn({ name: 'strategy_id' })
  strategy: Strategy;

  @Column({ type: 'enum', enum: PositionSide, default: PositionSide.LONG })
  side: PositionSide;

  @Column({ type: 'enum', enum: PositionStatus, default: PositionStatus.OPEN })
  status: PositionStatus;

  @Column({ name: 'entry_price', type: 'decimal', precision: 18, scale: 8 })
  entryPrice: number;

  @Column({ name: 'current_price', type: 'decimal', precision: 18, scale: 8, nullable: true })
  currentPrice: number;

  @Column({ name: 'exit_price', type: 'decimal', precision: 18, scale: 8, nullable: true })
  exitPrice: number;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  quantity: number;

  @Column({ name: 'unrealized_pl', type: 'decimal', precision: 18, scale: 8, default: 0 })
  unrealizedPl: number;

  @Column({ name: 'realized_pl', type: 'decimal', precision: 18, scale: 8, default: 0 })
  realizedPl: number;

  @Column({ name: 'is_probation', default: true })
  isProbation: boolean;

  @CreateDateColumn({ name: 'opened_at' })
  openedAt: Date;

  @Column({ name: 'closed_at', type: 'timestamptz', nullable: true })
  closedAt: Date;

  @OneToMany(() => Order, (order) => order.position)
  orders: Order[];
}
