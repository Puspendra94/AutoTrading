import { MigrationInterface, QueryRunner } from 'typeorm';

// cost_usd is computed from LlmService's PRICING table, which only carries Claude models — a
// DeepSeek decision therefore logs $0.00 rather than a fabricated estimate. That is the right
// call (an invented price is worse than none), but it leaves no usage signal at all.
//
// Token counts are a FACT reported by the provider, independent of any price lookup, so they are
// stored directly. Multiply by the real rate later to get spend; until then they still answer
// "is this prompt getting more expensive?".
export class AddPatternDecisionTokens1700000019000 implements MigrationInterface {
  name = 'AddPatternDecisionTokens1700000019000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."pattern_decisions"
        ADD COLUMN IF NOT EXISTS "input_tokens" integer NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS "output_tokens" integer NOT NULL DEFAULT 0;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."pattern_decisions"
        DROP COLUMN IF EXISTS "output_tokens",
        DROP COLUMN IF EXISTS "input_tokens";
    `);
  }
}
