import { MigrationInterface, QueryRunner } from 'typeorm';

// Optional whipsaw guard on the promotion gate: the minimum average out-of-sample hold, in
// eval-interval bars, a strategy must clear to be promoted. Nullable and NULL by default so
// existing promotion behavior is unchanged until an operator sets a threshold — at which point
// over-trading strategies that churn in and out on noise (the failure mode observed live:
// hundreds of tiny fee-bleeding round-trips) are rejected before going live.
export class AddMinAvgHoldBarsToPolicy1700000013000 implements MigrationInterface {
  name = 'AddMinAvgHoldBarsToPolicy1700000013000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."strategy_evaluation_policy"
        ADD COLUMN "min_avg_hold_bars" numeric(6,2);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."strategy_evaluation_policy"
        DROP COLUMN "min_avg_hold_bars";
    `);
  }
}
