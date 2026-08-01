import { MigrationInterface, QueryRunner } from 'typeorm';

// Move every ticker onto the futures venue.
//
// WHY: this system decides both directions, and a SHORT is a SELL-to-open that only futures
// accepts. On a spot ticker roughly half the strategy's signals are structurally unexecutable —
// they fill happily in paper and would be rejected by the exchange in live, which is the worst
// possible place to discover it. At 1x leverage a futures position is economically identical to
// the spot one it replaces, so nothing changes on the long side.
//
// WHY THIS IS NEEDED AT ALL: AddFuturesSupport (…14000) seeds a `futures` market type by selecting
// rows that already have `spot`. main.ts runs migrations BEFORE MarketDataSeedService creates
// anything, so on a fresh database market_types is still empty at that moment and that INSERT
// matches nothing. Every clean install therefore ended up spot-only and silently unable to short.
// The seed service now creates the futures type directly; this repairs databases already built.
//
// Two subtleties this has to handle:
//   * market_types has no unique constraint on (provider_id, name), so inserts must guard
//     themselves or a re-run would duplicate rows.
//   * a ticker's market_type may belong to a DIFFERENT provider than the ticker itself — that is
//     the state a manually repointed ticker is left in. The futures type is created under the
//     TICKER's own provider so the two agree afterwards.
export class MoveTickersToFutures1700000021000 implements MigrationInterface {
  name = 'MoveTickersToFutures1700000021000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    // 1. Every provider that owns a ticker gets a futures market type, if it lacks one.
    await queryRunner.query(`
      INSERT INTO "Algo_Trading"."market_types" ("provider_id", "name")
      SELECT DISTINCT t."provider_id", 'futures'
      FROM "Algo_Trading"."tickers" t
      WHERE NOT EXISTS (
        SELECT 1 FROM "Algo_Trading"."market_types" mt
        WHERE mt."provider_id" = t."provider_id" AND mt."name" = 'futures'
      );
    `);

    // 2. Point every ticker at its OWN provider's futures type. Idempotent: tickers already
    //    correct are excluded by the final predicate.
    await queryRunner.query(`
      UPDATE "Algo_Trading"."tickers" t
      SET "market_type_id" = mt."id"
      FROM "Algo_Trading"."market_types" mt
      WHERE mt."provider_id" = t."provider_id"
        AND mt."name" = 'futures'
        AND (t."market_type_id" IS NULL OR t."market_type_id" <> mt."id");
    `);
  }

  public async down(): Promise<void> {
    // Deliberately not reversed. Rolling back would mean guessing which tickers had been on spot
    // before, and putting a ticker back on spot would re-break short execution — a worse state
    // than the one being rolled back from. Repoint by hand if that is genuinely wanted.
  }
}
