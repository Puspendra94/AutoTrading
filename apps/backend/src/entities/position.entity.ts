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

// How a trade was executed, stamped at fill time from the executing provider.
export enum TradeMode {
  PAPER = 'paper', // simulated local fill — never sent to any exchange
  LIVE = 'live', // real order placed on Binance
}

// The Binance network a LIVE order hit. NULL for paper trades (they call no API).
export enum TradeNetwork {
  TESTNET = 'testnet',
  MAINNET = 'mainnet',
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

  // Set at fill time so the Live Trades panel can show only trades matching the active
  // Mode + Network. `network` is null for paper trades (network-agnostic).
  @Column({ name: 'trade_mode', type: 'enum', enum: TradeMode, default: TradeMode.PAPER })
  tradeMode: TradeMode;

  @Column({ name: 'network', type: 'enum', enum: TradeNetwork, nullable: true })
  network: TradeNetwork | null;

  // timestamptz (not the bare @CreateDateColumn default of `timestamp without time zone`) so the
  // stored instant round-trips as UTC — otherwise the driver re-interprets the naive wall-clock
  // as the server's local zone on read, shifting every "opened X ago" by the local offset.
  @CreateDateColumn({ name: 'opened_at', type: 'timestamptz' })
  openedAt: Date;

  @Column({ name: 'closed_at', type: 'timestamptz', nullable: true })
  closedAt: Date;

  @OneToMany(() => Order, (order) => order.position)
  orders: Order[];
}
