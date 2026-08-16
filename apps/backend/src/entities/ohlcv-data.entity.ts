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

  // Every remaining field Binance sends with a kline. Nullable because 3.6M rows predate them
  // and stay NULL until a re-backfill; treat NULL as "not known", never as zero — a zero
  // takerBuyBase would read as 100% aggressive selling, which is a real signal and a fabricated one.
  @Column({ name: 'quote_volume', type: 'decimal', precision: 24, scale: 8, nullable: true })
  quoteVolume: number | null;

  @Column({ type: 'int', nullable: true })
  trades: number | null;

  // Volume that lifted the offer. takerBuyBase / volume is order-flow imbalance — information a
  // candle does not contain, since two bars with identical OHLC can have opposite flow.
  @Column({ name: 'taker_buy_base', type: 'decimal', precision: 24, scale: 8, nullable: true })
  takerBuyBase: number | null;

  @Column({ name: 'taker_buy_quote', type: 'decimal', precision: 24, scale: 8, nullable: true })
  takerBuyQuote: number | null;
}
