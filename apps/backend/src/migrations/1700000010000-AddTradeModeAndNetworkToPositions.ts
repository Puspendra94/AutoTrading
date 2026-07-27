import { MigrationInterface, QueryRunner } from 'typeorm';

// A trade must record HOW it was executed so the dashboard can show only the trades that
// match the currently-selected Mode + Network:
//   * trade_mode ('paper' | 'live') — 'paper' is a simulated local fill that never touches
//     any exchange; 'live' was sent to Binance.
//   * network   ('testnet' | 'mainnet') — the Binance network a LIVE order hit. NULL for
//     paper trades, which are network-agnostic (they call no API).
// Both are stamped at fill time from the executing provider's tradingMode/useTestnet.
export class AddTradeModeAndNetworkToPositions1700000010000 implements MigrationInterface {
  name = 'AddTradeModeAndNetworkToPositions1700000010000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `CREATE TYPE "Algo_Trading"."positions_trade_mode_enum" AS ENUM('paper', 'live');`,
    );
    await queryRunner.query(
      `CREATE TYPE "Algo_Trading"."positions_network_enum" AS ENUM('testnet', 'mainnet');`,
    );
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ADD COLUMN "trade_mode" "Algo_Trading"."positions_trade_mode_enum" NOT NULL DEFAULT 'paper';`,
    );
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ADD COLUMN "network" "Algo_Trading"."positions_network_enum";`,
    );

    // Backfill any pre-existing rows from their orders: a SIM_* provider order id means the
    // fill was simulated (paper); anything else was a real exchange order (live). Network is
    // left NULL for backfilled rows since it was never recorded before this migration.
    await queryRunner.query(`
      UPDATE "Algo_Trading"."positions" p
      SET "trade_mode" = 'live'
      WHERE EXISTS (
        SELECT 1 FROM "Algo_Trading"."orders" o
        WHERE o."position_id" = p."id" AND COALESCE(o."provider_order_id", '') NOT LIKE 'SIM_%'
      );
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."positions" DROP COLUMN "network";`);
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."positions" DROP COLUMN "trade_mode";`);
    await queryRunner.query(`DROP TYPE "Algo_Trading"."positions_network_enum";`);
    await queryRunner.query(`DROP TYPE "Algo_Trading"."positions_trade_mode_enum";`);
  }
}
