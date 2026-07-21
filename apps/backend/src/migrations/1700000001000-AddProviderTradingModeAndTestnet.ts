import { MigrationInterface, QueryRunner } from 'typeorm';

// Adds the explicit paper/live + testnet/mainnet safety switches (spec Section 0.2
// checkpoint — see PROGRESS.md "Deviations" for why these exist beyond the literal
// spec schema). Additive-only migration, never edits the initial schema migration.
export class AddProviderTradingModeAndTestnet1700000001000 implements MigrationInterface {
  name = 'AddProviderTradingModeAndTestnet1700000001000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."trading_mode_enum" AS ENUM('paper', 'live');
    `);
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."providers"
        ADD COLUMN "trading_mode" "Algo_Trading"."trading_mode_enum" NOT NULL DEFAULT 'paper',
        ADD COLUMN "use_testnet" boolean NOT NULL DEFAULT true;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."providers"
        DROP COLUMN "trading_mode",
        DROP COLUMN "use_testnet";
    `);
    await queryRunner.query(`DROP TYPE "Algo_Trading"."trading_mode_enum";`);
  }
}
