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

  @CreateDateColumn({ name: 'created_at' })
  createdAt: Date;
}
