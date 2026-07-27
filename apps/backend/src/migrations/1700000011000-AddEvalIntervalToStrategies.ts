import { MigrationInterface, QueryRunner } from 'typeorm';

// Per-strategy evaluation timeframe. Previously the live signal path used the global
// STRATEGY_EVAL_INTERVAL even though the generation planner could pick a different interval — a
// latent "what's backtested isn't what trades live" mismatch. Storing the interval on the
// strategy fixes that and lets the timeframe be chosen per generation run (incl. fast 5m/15m for
// quick testing). Nullable: existing rows fall back to the config default.
export class AddEvalIntervalToStrategies1700000011000 implements MigrationInterface {
  name = 'AddEvalIntervalToStrategies1700000011000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."strategies" ADD COLUMN "eval_interval" varchar;`);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."strategies" DROP COLUMN "eval_interval";`);
  }
}
