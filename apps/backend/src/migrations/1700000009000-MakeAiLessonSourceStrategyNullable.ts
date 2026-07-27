import { MigrationInterface, QueryRunner } from 'typeorm';

// Failure lessons (spec 4.11 continuous learning) are recorded when a whole generation cycle
// fails the evaluation gate. Because of the gate-before-save contract, a failed cycle persists
// NO strategy row — so its lesson has no source_strategy_id. Relax the NOT NULL so those
// "what went wrong" lessons can be stored and fed back into the next generation prompt.
export class MakeAiLessonSourceStrategyNullable1700000009000 implements MigrationInterface {
  name = 'MakeAiLessonSourceStrategyNullable1700000009000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."ai_lessons_learned" ALTER COLUMN "source_strategy_id" DROP NOT NULL;`,
    );
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    // Failure lessons have a null source_strategy_id and can't satisfy a restored NOT NULL —
    // remove them before re-imposing the constraint so the down migration is clean.
    await queryRunner.query(
      `DELETE FROM "Algo_Trading"."ai_lessons_learned" WHERE "source_strategy_id" IS NULL;`,
    );
    await queryRunner.query(
      `ALTER TABLE "Algo_Trading"."ai_lessons_learned" ALTER COLUMN "source_strategy_id" SET NOT NULL;`,
    );
  }
}
