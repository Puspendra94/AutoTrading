import { Entity, PrimaryGeneratedColumn, Column, CreateDateColumn, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';
import { MarketType } from './market-type.entity';

export enum TickerStatus {
  ACTIVE = 'active',
  INACTIVE = 'inactive',
  ONBOARDING = 'onboarding',
}

export enum OnboardingStage {
  FETCHING_HISTORY = 'fetching_history',
  GENERATING_STRATEGY = 'generating_strategy',
  BACKTESTING = 'backtesting',
  EVALUATING = 'evaluating',
  READY = 'ready',
  FAILED = 'failed',
}

@Entity({ name: 'tickers', schema: 'Algo_Trading' })
export class Ticker {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column({ name: 'market_type_id', nullable: true })
  marketTypeId: string;

  @ManyToOne(() => MarketType, { onDelete: 'SET NULL', nullable: true })
  @JoinColumn({ name: 'market_type_id' })
  marketType: MarketType;

  @Column()
  symbol: string; // e.g. "BTCUSDT"

  @Column({ default: '1m' })
  interval: string; // e.g. "1m", "5m", "1h", "1d"

  @Column({ type: 'enum', enum: TickerStatus, default: TickerStatus.ONBOARDING })
  status: TickerStatus;

  @Column({ type: 'enum', enum: OnboardingStage, default: OnboardingStage.FETCHING_HISTORY })
  onboardingStage: OnboardingStage;

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;
}
