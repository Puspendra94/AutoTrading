import { MigrationInterface, QueryRunner } from 'typeorm';

// Chart signals ("intents") used to be recomputed on every request and never stored. That was
// deterministic — same candles + same rule tree => same output — but only accidentally stable:
// the request replays the LAST N candles, so as the window slides, signals older than the window
// silently disappear and the replay's starting bar moves.
//
// Persisting them makes the set stable by DESIGN: switching interval and coming back, or
// reloading the page, returns exactly what was there before, and history is retained after the
// candles that produced it fall out of the replay window.
//
// Keyed by (strategy, interval, bar, side) so a re-replay of the same bar is an idempotent
// upsert rather than a duplicate. `interval` is part of the key because the same strategy
// legitimately produces different markers on different display timeframes.
export class AddStrategySignals1700000015000 implements MigrationInterface {
  name = 'AddStrategySignals1700000015000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."strategy_signals" (
        "id" uuid NOT NULL DEFAULT uuid_generate_v4(),
        "strategy_id" uuid NOT NULL,
        "ticker_id" uuid NOT NULL,
        "interval" character varying NOT NULL,
        "bar_time" TIMESTAMP WITH TIME ZONE NOT NULL,
        "side" character varying NOT NULL,
        "direction" character varying NOT NULL DEFAULT 'long',
        "price" numeric(18,8) NOT NULL,
        "reason" character varying NOT NULL DEFAULT '',
        "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT "PK_strategy_signals" PRIMARY KEY ("id"),
        CONSTRAINT "UQ_strategy_signals_bar" UNIQUE ("strategy_id", "interval", "bar_time", "side"),
        CONSTRAINT "FK_strategy_signals_strategy" FOREIGN KEY ("strategy_id")
          REFERENCES "Algo_Trading"."strategies"("id") ON DELETE CASCADE,
        CONSTRAINT "FK_strategy_signals_ticker" FOREIGN KEY ("ticker_id")
          REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE
      );
    `);
    // The read path is always "this strategy, this interval, in bar order".
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_strategy_signals_lookup"
      ON "Algo_Trading"."strategy_signals" ("strategy_id", "interval", "bar_time");
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP INDEX IF EXISTS "Algo_Trading"."IDX_strategy_signals_lookup";`);
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."strategy_signals";`);
  }
}
