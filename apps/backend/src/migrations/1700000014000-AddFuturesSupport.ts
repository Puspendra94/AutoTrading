import { MigrationInterface, QueryRunner } from 'typeorm';

// Phase 4b — live futures (short-side) execution.
//
//  * positions.leverage / positions.margin_type: recorded at fill time so a futures position
//    carries how it was sized. Default 1x / isolated — at 1x a futures position is economically
//    a spot position, so existing rows and the spot path are unaffected.
//  * Seed a `futures` market type under the same provider that owns the existing `spot` one.
//    A ticker pointed at this market type (a) unlocks SHORT strategy generation
//    (generator.ts allow_short = market_type === 'futures') and (b) routes live orders through
//    the USD-M futures adapter. Repointing a specific ticker is an operational step (data, not
//    schema) done outside this migration.
export class AddFuturesSupport1700000014000 implements MigrationInterface {
  name = 'AddFuturesSupport1700000014000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ADD COLUMN IF NOT EXISTS "leverage" integer NOT NULL DEFAULT 1;`,
    );
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ADD COLUMN IF NOT EXISTS "margin_type" varchar NOT NULL DEFAULT 'isolated';`,
    );

    // Seed a `futures` market type under whichever provider already owns `spot`, so the futures
    // ticker lives beside the spot one on the same Binance provider. No-op if it already exists.
    await queryRunner.query(`
      INSERT INTO "Algo_Trading"."market_types" ("provider_id", "name")
      SELECT DISTINCT "provider_id", 'futures'
      FROM "Algo_Trading"."market_types"
      WHERE "name" = 'spot'
        AND NOT EXISTS (
          SELECT 1 FROM "Algo_Trading"."market_types" m2
          WHERE m2."name" = 'futures' AND m2."provider_id" = "market_types"."provider_id"
        );
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DELETE FROM "Algo_Trading"."market_types" WHERE "name" = 'futures';`);
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."positions" DROP COLUMN IF EXISTS "margin_type";`);
    await queryRunner.query(`ALTER TABLE "Algo_Trading"."positions" DROP COLUMN IF EXISTS "leverage";`);
  }
}
