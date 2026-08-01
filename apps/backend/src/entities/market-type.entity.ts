import { Entity, PrimaryGeneratedColumn, Column, ManyToOne, JoinColumn } from 'typeorm';
import { Provider } from './provider.entity';

/**
 * The venue every new ticker is created on.
 *
 * Futures, not spot, and deliberately so: this system trades both directions, and a SHORT is a
 * SELL-to-open that only the futures venue accepts. On spot roughly half the strategy's signals
 * are structurally unexecutable — they simulate fine in paper and then fail at the exchange, which
 * is the worst way to find out.
 *
 * At 1x leverage a futures position is economically the same as the spot one it replaces, so this
 * costs nothing on the long side.
 */
export const DEFAULT_MARKET_TYPE = 'futures';

@Entity({ name: 'market_types', schema: 'Algo_Trading' })
export class MarketType {
  @PrimaryGeneratedColumn('uuid')
  id: string;

  @Column({ name: 'provider_id' })
  providerId: string;

  @ManyToOne(() => Provider, { onDelete: 'CASCADE' })
  @JoinColumn({ name: 'provider_id' })
  provider: Provider;

  @Column()
  name: string; // e.g. "spot", "futures", "options"
}
