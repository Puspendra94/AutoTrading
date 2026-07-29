import { MigrationInterface, QueryRunner } from 'typeorm';

// Chart markers for the pattern brain (TRADING_BRAIN=pattern).
//
// Deliberately a SEPARATE table from `strategy_signals` rather than reusing it with a nullable
// `strategy_id`. Two reasons, both load-bearing:
//
//  1. `strategy_signals` is keyed UNIQUE (strategy_id, interval, bar_time, side). Postgres treats
//     every NULL as distinct, so a NULL strategy_id would make ON CONFLICT DO NOTHING a no-op and
//     the same trigger would be re-inserted on every write — duplicate markers, forever.
//  2. The strategy brain's signals consumer REPLAYS the live strategy and rewrites that table on
//     every chart request. Sharing it would mean the two brains fighting over the same rows.
//
// A trigger is stored ONCE, at the interval the feature engine actually evaluates (15m). The read
// path buckets bar_time down to whatever timeframe the chart is displaying, so one row serves
// every display interval instead of being duplicated across all of them.
export class AddPatternSignals1700000017000 implements MigrationInterface {
  name = 'AddPatternSignals1700000017000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."pattern_signals" (
        "id" uuid NOT NULL DEFAULT uuid_generate_v4(),
        "ticker_id" uuid NOT NULL,
        "interval" character varying NOT NULL,
        "bar_time" TIMESTAMP WITH TIME ZONE NOT NULL,
        "kind" character varying NOT NULL,
        "side" character varying NOT NULL,
        "price" numeric(18,8) NOT NULL,
        "detail" character varying NOT NULL DEFAULT '',
        "state_pack" jsonb,
        "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT "PK_pattern_signals" PRIMARY KEY ("id"),
        CONSTRAINT "UQ_pattern_signals_bar" UNIQUE ("ticker_id", "interval", "bar_time", "kind"),
        CONSTRAINT "FK_pattern_signals_ticker" FOREIGN KEY ("ticker_id")
          REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE
      );
    `);
    // Every column of the unique key is NOT NULL, so re-detecting the same trigger on the same
    // bar is a genuine no-op upsert — the failure mode described above cannot occur here.
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_pattern_signals_lookup"
      ON "Algo_Trading"."pattern_signals" ("ticker_id", "bar_time" DESC);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP INDEX IF EXISTS "Algo_Trading"."IDX_pattern_signals_lookup";`);
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."pattern_signals";`);
  }
}
