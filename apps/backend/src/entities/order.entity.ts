import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Position } from './position.entity';

export enum OrderSide {
  BUY = 'buy',
  SELL = 'sell',
}

export enum OrderType {
  MARKET = 'market',
  LIMIT = 'limit',
  STOP_LOSS = 'stop_loss',
  TAKE_PROFIT = 'take_profit',
}

export enum OrderStatus {
  PENDING = 'pending',
  FILLED = 'filled',
  PARTIAL = 'partial',
  REJECTED = 'rejected',
  CANCELLED = 'cancelled',
}

@Entity({ name: 'orders', schema: 'Algo_Trading' })
export class Order {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'position_id', nullable: true })
  positionId: string;

  @ManyToOne(() => Position, (position) => position.orders, { onDelete: 'SET NULL', nullable: true })
  @JoinColumn({ name: 'position_id' })
  position: Position;

  @Column({ name: 'provider_order_id', nullable: true })
  providerOrderId: string;

  @Column({ type: 'enum', enum: OrderSide })
  side: OrderSide;

  @Column({ type: 'decimal', precision: 18, scale: 8 })
  quantity: number;

  @Column({ type: 'decimal', precision: 18, scale: 8, nullable: true })
  price: number;

  @Column({ name: 'order_type', type: 'enum', enum: OrderType, default: OrderType.MARKET })
  orderType: OrderType;

  @Column({ type: 'enum', enum: OrderStatus, default: OrderStatus.PENDING })
  status: OrderStatus;

  @CreateDateColumn({ name: 'submitted_at' })
  submittedAt: Date;

  @Column({ name: 'filled_at', type: 'timestamptz', nullable: true })
  filledAt: Date;
}
