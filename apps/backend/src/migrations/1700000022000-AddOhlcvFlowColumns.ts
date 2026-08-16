import { MigrationInterface, QueryRunner } from 'typeorm';

// Keep every field Binance sends with a kline, not just the five we happen to use today.
//
// The parser read CSV columns 0-5 (open_time, OHLC, volume) and threw the rest away. Columns 7-10
// were being downloaded, decompressed, and discarded on every one of six years of backfill:
//
//   7  quote_asset_volume            volume denominated in USDT
//   8  number_of_trades              participation — 500 trades or one big one
//   9  taker_buy_base_asset_volume   volume that LIFTED THE OFFER
//  10  taker_buy_quote_asset_volume  the same in USDT
//
// Column 9 is the valuable one. taker_buy_volume / volume is order-flow imbalance: what share of
// the bar's volume was aggressive buying rather than aggressive selling. That is information a
// candle does not contain — two bars with identical OHLC can have opposite flow — and it is the
// one input available here that is not just another function of price.
//
// Storing them is cheap; NOT storing them is expensive, because adding a column later means
// re-downloading and re-parsing the entire history to populate it. Everything Binance sends now
// gets a column, so a future feature is a query rather than a six-year backfill.
//
// Nullable on purpose: 3.6M existing rows predate this and stay NULL until a re-backfill fills
// them. Anything reading these columns must treat NULL as "not known for this bar" rather than
// as zero — a zero taker_buy_volume would read as "100% aggressive selling", which is a real
// signal and would be entirely fabricated.
export class AddOhlcvFlowColumns1700000022000 implements MigrationInterface {
  name = 'AddOhlcvFlowColumns1700000022000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."ohlcv_data"
        ADD COLUMN IF NOT EXISTS "quote_volume" numeric(24,8),
        ADD COLUMN IF NOT EXISTS "trades" integer,
        ADD COLUMN IF NOT EXISTS "taker_buy_base" numeric(24,8),
        ADD COLUMN IF NOT EXISTS "taker_buy_quote" numeric(24,8);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."ohlcv_data"
        DROP COLUMN IF EXISTS "taker_buy_quote",
        DROP COLUMN IF EXISTS "taker_buy_base",
        DROP COLUMN IF EXISTS "trades",
        DROP COLUMN IF EXISTS "quote_volume";
    `);
  }
}
