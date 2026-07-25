import { MigrationInterface, QueryRunner } from 'typeorm';

// The Alpha Strategies UI surfaces each strategy's headline return and win rate, which
// the evaluator already derives from the out-of-sample trade series but never persisted.
// Additive-only, mirrors the pattern of 1700000004000 (parameter_count).
export class AddReturnAndWinRateToBacktestResults1700000007000 implements MigrationInterface {
  name = 'AddReturnAndWinRateToBacktestResults1700000007000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."backtest_results"
        ADD COLUMN "total_return_pct" numeric(10,2) NOT NULL DEFAULT 0,
        ADD COLUMN "win_rate" numeric(5,1) NOT NULL DEFAULT 0;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."backtest_results"
        DROP COLUMN "total_return_pct",
        DROP COLUMN "win_rate";
    `);
  }
}
