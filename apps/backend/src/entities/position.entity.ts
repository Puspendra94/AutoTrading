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

  // --- Pattern-brain exit ladder (null for strategy-brain positions, which use the
  // provider-level percentage caps instead). See worker/pattern/exits.py.
  //
  // `stopLoss` ratchets toward profit and never away from it. `initialStop` is frozen at entry so
  // a result can be measured in R against what the trade actually risked — a runner that trailed
  // to +3R did not make 0R just because its live stop reached breakeven.
  @Column({ name: 'stop_loss', type: 'decimal', precision: 18, scale: 8, nullable: true })
  stopLoss: number | null;

  // A soft target: reaching it does NOT close the position, it promotes it to 'runner' and
  // ratchets the stop to breakeven+fees.
  @Column({ name: 'take_profit', type: 'decimal', precision: 18, scale: 8, nullable: true })
  takeProfit: number | null;

  @Column({ name: 'initial_stop', type: 'decimal', precision: 18, scale: 8, nullable: true })
  initialStop: number | null;

  // Best price seen since entry — what the trailing stop is measured from.
  @Column({ name: 'extreme_price', type: 'decimal', precision: 18, scale: 8, nullable: true })
  extremePrice: number | null;

  // 'open' = fixed stop; 'runner' = past target, stop trailing.
  @Column({ default: 'open' })
  lifecycle: string;

  @Column({ name: 'exit_reason', type: 'varchar', nullable: true })
  exitReason: string | null;

  // Futures sizing, stamped at fill time. Default 1x / isolated — at 1x a futures position is
  // economically a spot position, so spot fills carry these defaults harmlessly.
  @Column({ name: 'leverage', type: 'int', default: 1 })
  leverage: number;

  @Column({ name: 'margin_type', default: 'isolated' })
  marginType: string;

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
