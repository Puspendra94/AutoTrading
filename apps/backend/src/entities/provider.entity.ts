import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn, OneToMany, OneToOne } from 'typeorm';
import { User } from './user.entity';

export enum ProviderType {
  BINANCE = 'binance',
  ZERODHA = 'zerodha',
  MOCK = 'mock',
}

export enum ProviderStatus {
  ACTIVE = 'active',
  INACTIVE = 'inactive',
  ERROR = 'error',
}

export enum TradingMode {
  PAPER = 'paper',
  LIVE = 'live',
}

@Entity({ name: 'providers', schema: 'Algo_Trading' })
export class Provider {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'user_id' })
  userId: string;

  @ManyToOne(() => User, (user) => user.providers, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'user_id' })
  user: User;

  @Column()
  name: string;

  @Column({ type: 'enum', enum: ProviderType, default: ProviderType.BINANCE })
  type: ProviderType;

  @Column({ type: 'enum', enum: ProviderStatus, default: ProviderStatus.ACTIVE })
  status: ProviderStatus;

  @Column({ name: 'trading_enabled', default: true })
  tradingEnabled: boolean;

  // Explicit safety switch (spec Section 0.2 checkpoint): order placement only ever
  // touches the real Binance API when this is 'live'. Defaults to 'paper' (simulated
  // fills against real streamed prices) until the user opts in deliberately.
  @Column({ name: 'trading_mode', type: 'enum', enum: TradingMode, default: TradingMode.PAPER })
  tradingMode: TradingMode;

  // When true (default), 'live' mode still targets Binance's Spot Testnet rather than
  // mainnet — both this AND tradingMode must be explicitly flipped for real capital to move.
  @Column({ name: 'use_testnet', default: true })
  useTestnet: boolean;

  // Real kill switch (spec 5.4) — distinct from the daily-loss-limit block, which only
  // halts until the next reset boundary. This is a hard stop until manually cleared,
  // triggered by a serious reconciliation mismatch or repeated provider API failures
  // (see RiskGateService/ReconciliationService), or manually via the API.
  @Column({ name: 'kill_switch_active', default: false })
  killSwitchActive: boolean;

  @Column({ name: 'kill_switch_reason', type: 'text', nullable: true })
  killSwitchReason: string | null;

  @Column({ name: 'api_failure_count', type: 'int', default: 0 })
  apiFailureCount: number;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;
}
