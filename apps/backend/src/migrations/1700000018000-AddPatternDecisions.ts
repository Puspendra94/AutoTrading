import { MigrationInterface, QueryRunner } from 'typeorm';

// Every bar the pattern brain evaluates produces exactly one row here — including the free ones
// where the gate declined and no LLM call was made.
//
// Logging the skips is the point. A gate that is too tight is otherwise invisible: the system
// simply never trades and nothing records why. With this table, "we saw 96 bars, skipped 91 for
// no trigger, asked the model 5 times, it declined 3, guardrails rejected 1, we traded 1" is a
// query rather than a guess.
//
// state_pack holds the exact feature state the decision was made on, so a past decision can be
// replayed against a changed prompt without recomputing indicators or re-paying for inference.
export class AddPatternDecisions1700000018000 implements MigrationInterface {
  name = 'AddPatternDecisions1700000018000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."pattern_decisions" (
        "id" uuid NOT NULL DEFAULT uuid_generate_v4(),
        "ticker_id" uuid NOT NULL,
        "interval" character varying NOT NULL,
        "bar_time" TIMESTAMP WITH TIME ZONE NOT NULL,
        "outcome" character varying NOT NULL,
        "reason" text NOT NULL DEFAULT '',
        "gate" jsonb,
        "state_pack" jsonb,
        "llm_decision" jsonb,
        "validation" jsonb,
        "sizing" jsonb,
        "position_id" uuid,
        "stop_price" numeric(18,8),
        "take_profit" numeric(18,8),
        "model" character varying,
        "cost_usd" numeric(12,6) NOT NULL DEFAULT 0,
        "created_at" TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        CONSTRAINT "PK_pattern_decisions" PRIMARY KEY ("id"),
        CONSTRAINT "UQ_pattern_decisions_bar" UNIQUE ("ticker_id", "interval", "bar_time"),
        CONSTRAINT "FK_pattern_decisions_ticker" FOREIGN KEY ("ticker_id")
          REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        CONSTRAINT "FK_pattern_decisions_position" FOREIGN KEY ("position_id")
          REFERENCES "Algo_Trading"."positions"("id") ON DELETE SET NULL
      );
    `);
    // One decision per bar, so re-processing a bar after a restart updates rather than duplicates.
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_pattern_decisions_lookup"
      ON "Algo_Trading"."pattern_decisions" ("ticker_id", "bar_time" DESC);
    `);
    // The dashboard feed filters to the decisions worth showing, newest first.
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_pattern_decisions_outcome"
      ON "Algo_Trading"."pattern_decisions" ("ticker_id", "outcome", "bar_time" DESC);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP INDEX IF EXISTS "Algo_Trading"."IDX_pattern_decisions_outcome";`);
    await queryRunner.query(`DROP INDEX IF EXISTS "Algo_Trading"."IDX_pattern_decisions_lookup";`);
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."pattern_decisions";`);
  }
}
