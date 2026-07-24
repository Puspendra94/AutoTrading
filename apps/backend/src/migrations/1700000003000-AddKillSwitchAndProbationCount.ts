import { MigrationInterface, QueryRunner } from 'typeorm';

// Closes two gaps found auditing against the spec: (1) a real kill switch (Section
// 5.4) — manually triggerable and auto-triggered from reconciliation mismatches /
// repeated provider API failures, not just the daily-loss-limit block that already
// existed; (2) probation sizing (Section 2.5/12) never actually expired — the trade
// count threshold needs to live on the policy (risk_limits), same as probation_size_pct,
// rather than only existing as an unread .env default. Additive-only, never edits the
// initial schema migration.
export class AddKillSwitchAndProbationCount1700000003000 implements MigrationInterface {
  name = 'AddKillSwitchAndProbationCount1700000003000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."providers"
        ADD COLUMN "kill_switch_active" boolean NOT NULL DEFAULT false,
        ADD COLUMN "kill_switch_reason" text,
        ADD COLUMN "api_failure_count" int NOT NULL DEFAULT 0;
    `);
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."risk_limits"
        ADD COLUMN "probation_trades_count" int NOT NULL DEFAULT 10;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."risk_limits"
        DROP COLUMN "probation_trades_count";
    `);
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."providers"
        DROP COLUMN "kill_switch_active",
        DROP COLUMN "kill_switch_reason",
        DROP COLUMN "api_failure_count";
    `);
  }
}
