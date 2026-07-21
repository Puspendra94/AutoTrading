import { MigrationInterface, QueryRunner } from 'typeorm';

// The only table from spec Section 8 that the initial migration was missing (Section 8.5).
export class AddAiLessonsLearned1700000002000 implements MigrationInterface {
  name = 'AddAiLessonsLearned1700000002000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."lesson_outcome_enum" AS ENUM('success', 'failure');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."ai_lessons_learned" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "source_strategy_id" uuid NOT NULL REFERENCES "Algo_Trading"."strategies"("id") ON DELETE CASCADE,
        "source_divergence_id" uuid REFERENCES "Algo_Trading"."live_vs_backtest_divergence"("id") ON DELETE SET NULL,
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "market_type" varchar,
        "strategy_type" varchar,
        "regime_tags" jsonb,
        "outcome" "Algo_Trading"."lesson_outcome_enum" NOT NULL,
        "summary_text" text NOT NULL,
        "created_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "idx_ai_lessons_ticker" ON "Algo_Trading"."ai_lessons_learned" ("ticker_id");
      CREATE INDEX IF NOT EXISTS "idx_ai_lessons_strategy_type" ON "Algo_Trading"."ai_lessons_learned" ("strategy_type");
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."ai_lessons_learned";`);
    await queryRunner.query(`DROP TYPE IF EXISTS "Algo_Trading"."lesson_outcome_enum";`);
  }
}
