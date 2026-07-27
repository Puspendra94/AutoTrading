import { MigrationInterface, QueryRunner } from 'typeorm';

// `positions.opened_at` was created by a bare @CreateDateColumn, which maps to
// `timestamp without time zone`. The app writes UTC instants, but on read the pg driver
// re-interprets the naive value in the server process's local zone, shifting every
// "opened X ago" by the local UTC offset (e.g. +5:30 → the open trade showed "7h ago"
// when it opened ~1.5h ago). `closed_at` was already `timestamptz` and read correctly.
// Convert opened_at to timestamptz, interpreting the existing naive values as UTC (which is
// what the app actually stored), so historical rows keep the correct instant.
export class MakePositionOpenedAtTimestamptz1700000012000 implements MigrationInterface {
  name = 'MakePositionOpenedAtTimestamptz1700000012000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ALTER COLUMN "opened_at" TYPE timestamptz USING "opened_at" AT TIME ZONE 'UTC';`,
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."positions" ALTER COLUMN "opened_at" TYPE timestamp USING "opened_at" AT TIME ZONE 'UTC';`,
    );
  }
}
