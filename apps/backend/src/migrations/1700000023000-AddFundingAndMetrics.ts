import { MigrationInterface, QueryRunner } from 'typeorm';

// Derivatives data that has no equivalent in a candle.
//
// Everything the system reads today is price and volume. Funding and open interest describe
// POSITIONING — who is holding what, and what it costs them to keep holding it — which is a
// different question from what price did:
//
//   funding_rate     Longs pay shorts (positive) or shorts pay longs (negative), every 8h.
//                    Persistently positive funding means the long side is crowded and paying to
//                    stay. That is a cost the crowd carries, and historically a reversion signal.
//
//   open_interest    Contracts outstanding. The same 1% rally means opposite things depending on
//                    whether OI rose (new money taking risk) or fell (shorts being squeezed out
//                    — a move with no one left to fuel it).
//
//   long/short ratios  How retail and how TOP traders are positioned, by account count and by
//                    position size. The gap between "most accounts are long" and "most SIZE is
//                    short" is information price cannot express.
//
// Two tables rather than columns on ohlcv_data, because the grains differ: funding settles every
// 8 hours, metrics are published every 5 minutes, and bars are 1 minute. Forcing them into the
// bar table would mean either fabricating values between publications or leaving most rows NULL.
// Consumers join AS-OF — the latest value at or before the bar — which is also the only join that
// cannot see the future.
//
// Archive coverage differs too: funding goes back to 2020-01 as monthly files, metrics only to
// ~2021 and daily-only. Both are nullable-by-absence; a bar with no metrics row is a bar where
// that information did not exist yet, which is not the same as zero.
export class AddFundingAndMetrics1700000023000 implements MigrationInterface {
  name = 'AddFundingAndMetrics1700000023000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."funding_rate" (
        "ticker_id"      uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "timestamp"      timestamptz NOT NULL,
        "rate"           numeric(18,10) NOT NULL,
        "interval_hours" integer,
        PRIMARY KEY ("ticker_id", "timestamp")
      );
    `);
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."futures_metrics" (
        "ticker_id"                uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "timestamp"                timestamptz NOT NULL,
        "open_interest"            numeric(24,8),
        "open_interest_value"      numeric(28,8),
        -- "count_" ratios are by ACCOUNT, "sum_" ratios are by POSITION SIZE. Both are kept:
        -- many small accounts long while large positions are short is a real, tradeable divergence,
        -- and collapsing them to one number would erase exactly that.
        "toptrader_ratio_accounts" numeric(18,8),
        "toptrader_ratio_positions" numeric(18,8),
        "global_ratio_accounts"    numeric(18,8),
        "taker_buy_sell_ratio"     numeric(18,8),
        PRIMARY KEY ("ticker_id", "timestamp")
      );
    `);
    // Both are read as "the newest row at or before this bar", which is an index-backed
    // ORDER BY ... DESC LIMIT 1 per lookup.
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_funding_rate_lookup"
      ON "Algo_Trading"."funding_rate" ("ticker_id", "timestamp" DESC);
    `);
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_futures_metrics_lookup"
      ON "Algo_Trading"."futures_metrics" ("ticker_id", "timestamp" DESC);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."futures_metrics";`);
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."funding_rate";`);
  }
}
