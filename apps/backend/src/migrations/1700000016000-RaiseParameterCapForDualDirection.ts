import { MigrationInterface, QueryRunner } from 'typeorm';

// A symmetric two-sided ('both') strategy reuses the same indicators and periods on each side and
// only mirrors the thresholds, so it costs roughly ONE extra tunable knob over a single-sided
// strategy — not double. Measured on a real RSI/EMA pair: long-only 6 knobs, the same strategy
// trading both sides 7.
//
// The cap stays the primary overfitting defence, so this is a deliberately small bump (6 -> 8):
// enough headroom for a mirrored short leg plus a trailing stop, not enough for two unrelated
// strategies bolted together (which is exactly what we want the cap to keep rejecting).
export class RaiseParameterCapForDualDirection1700000016000 implements MigrationInterface {
  name = 'RaiseParameterCapForDualDirection1700000016000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `UPDATE "Algo_Trading"."strategy_evaluation_policy" SET "max_parameter_count" = 8 WHERE "max_parameter_count" < 8;`,
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `UPDATE "Algo_Trading"."strategy_evaluation_policy" SET "max_parameter_count" = 6 WHERE "max_parameter_count" = 8;`,
    );
  }
}
