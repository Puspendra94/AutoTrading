import { MigrationInterface, QueryRunner } from 'typeorm';

// Provider-level hard stop-loss / take-profit guardrails (configurable from the Profile
// page). Unlike the per-strategy stopLossPct the LLM proposes, these are a hard cap the
// live loop enforces on EVERY open position regardless of strategy — so a position always
// has a floor. Nullable: null means "no hard cap, rely on the strategy's own exits".
export class AddHardStopLossToRiskLimits1700000006000 implements MigrationInterface {
  name = 'AddHardStopLossToRiskLimits1700000006000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."risk_limits"
        ADD COLUMN "hard_stop_loss_pct" numeric(5,2),
        ADD COLUMN "hard_take_profit_pct" numeric(5,2);
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."risk_limits"
        DROP COLUMN "hard_stop_loss_pct",
        DROP COLUMN "hard_take_profit_pct";
    `);
  }
}
