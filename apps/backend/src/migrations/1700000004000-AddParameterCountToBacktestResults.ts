import { MigrationInterface, QueryRunner } from 'typeorm';

// Overfitting check (spec 7.2) — the count of tunable parameters a generated strategy
// used now factors into the promotion gate (strategy-evaluator.service.ts) and needs to
// be persisted alongside the other backtest metrics. Additive-only, never edits an
// already-run migration (1700000003000 has already executed against the dev DB).
export class AddParameterCountToBacktestResults1700000004000 implements MigrationInterface {
  name = 'AddParameterCountToBacktestResults1700000004000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."backtest_results"
        ADD COLUMN "parameter_count" int NOT NULL DEFAULT 0;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."backtest_results"
        DROP COLUMN "parameter_count";
    `);
  }
}
