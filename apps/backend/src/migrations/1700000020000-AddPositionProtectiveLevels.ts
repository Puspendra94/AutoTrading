import { MigrationInterface, QueryRunner } from 'typeorm';

// Per-position protective levels for the pattern brain's exit ladder.
//
// Until now the only exits were the PROVIDER-level hardStopLossPct/hardTakeProfitPct — one pair of
// percentages applied to every position on the account. That cannot express "this trade's stop is
// below the swing low that invalidates it", which is the whole point of sizing a trade against a
// specific level.
//
// `lifecycle` is what makes "let profit run" safe. A position starts 'open' with a fixed stop; when
// price crosses the target it becomes a 'runner' — the target does NOT close it. Instead the stop
// ratchets to breakeven and then trails. Without that, "wait for the next reversal" means a winner
// can round-trip all the way back to the original stop, which is exactly the 8%-becomes-a-loss
// scenario this is meant to prevent.
//
// `extreme_price` is the best price seen since entry (highest high for a long, lowest low for a
// short) and is what the trailing stop is measured from.
export class AddPositionProtectiveLevels1700000020000 implements MigrationInterface {
  name = 'AddPositionProtectiveLevels1700000020000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."positions"
        ADD COLUMN IF NOT EXISTS "stop_loss" numeric(18,8),
        ADD COLUMN IF NOT EXISTS "take_profit" numeric(18,8),
        ADD COLUMN IF NOT EXISTS "initial_stop" numeric(18,8),
        ADD COLUMN IF NOT EXISTS "extreme_price" numeric(18,8),
        ADD COLUMN IF NOT EXISTS "lifecycle" character varying NOT NULL DEFAULT 'open',
        ADD COLUMN IF NOT EXISTS "exit_reason" character varying;
    `);
    // The tick loop asks "which of this ticker's positions still need managing" on every tick, so
    // the open subset is worth indexing directly.
    await queryRunner.query(`
      CREATE INDEX IF NOT EXISTS "IDX_positions_open_lifecycle"
      ON "Algo_Trading"."positions" ("ticker_id", "lifecycle") WHERE "status" = 'open';
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP INDEX IF EXISTS "Algo_Trading"."IDX_positions_open_lifecycle";`);
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."positions"
        DROP COLUMN IF EXISTS "exit_reason",
        DROP COLUMN IF EXISTS "lifecycle",
        DROP COLUMN IF EXISTS "extreme_price",
        DROP COLUMN IF EXISTS "initial_stop",
        DROP COLUMN IF EXISTS "take_profit",
        DROP COLUMN IF EXISTS "stop_loss";
    `);
  }
}
