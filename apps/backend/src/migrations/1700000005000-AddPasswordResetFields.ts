import { MigrationInterface, QueryRunner } from 'typeorm';

// First-party forgot/reset-password flow (no third-party auth). Adds a single-use reset
// token hash + expiry to users. Additive-only, nullable — existing rows are unaffected.
export class AddPasswordResetFields1700000005000 implements MigrationInterface {
  name = 'AddPasswordResetFields1700000005000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."users"
        ADD COLUMN "password_reset_token_hash" text,
        ADD COLUMN "password_reset_expires_at" timestamptz;
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      ALTER TABLE "Algo_Trading"."users"
        DROP COLUMN "password_reset_token_hash",
        DROP COLUMN "password_reset_expires_at";
    `);
  }
}
