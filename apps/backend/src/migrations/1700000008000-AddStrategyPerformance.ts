import { MigrationInterface, QueryRunner } from 'typeorm';

// Full-history "real data" performance replay per strategy (intents -> trades -> P/L -> drawdown),
// computed in the background. Additive-only; mirrors the pattern of the prior backtest migrations.
export class AddStrategyPerformance1700000008000 implements MigrationInterface {
  name = 'AddStrategyPerformance1700000008000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`
      DO $$ BEGIN
        CREATE TYPE "Algo_Trading"."strategy_performance_status_enum" AS ENUM ('pending', 'ready', 'failed');
      EXCEPTION WHEN duplicate_object THEN null; END $$;
    `);
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."strategy_performance" (
        "id" uuid NOT NULL DEFAULT uuid_generate_v4(),
        "strategy_id" uuid NOT NULL,
        "status" "Algo_Trading"."strategy_performance_status_enum" NOT NULL DEFAULT 'pending',
        "data_from" timestamptz,
        "data_to" timestamptz,
        "candle_count" bigint NOT NULL DEFAULT 0,
        "intent_count" int NOT NULL DEFAULT 0,
        "trade_count" int NOT NULL DEFAULT 0,
        "open_positions" int NOT NULL DEFAULT 0,
        "win_rate" numeric(5,1) NOT NULL DEFAULT 0,
        "total_return_pct" numeric(14,2) NOT NULL DEFAULT 0,
        "total_pnl_usd" numeric(18,2) NOT NULL DEFAULT 0,
        "max_drawdown_pct" numeric(10,2) NOT NULL DEFAULT 0,
        "sharpe" numeric(10,2) NOT NULL DEFAULT 0,
        "avg_trade_return_pct" numeric(10,3) NOT NULL DEFAULT 0,
        "notional_usd" numeric(18,2) NOT NULL DEFAULT 10000,
        "trades_json" jsonb,
        "error" text,
        "computed_at" timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT "PK_strategy_performance" PRIMARY KEY ("id"),
        CONSTRAINT "UQ_strategy_performance_strategy" UNIQUE ("strategy_id"),
        CONSTRAINT "FK_strategy_performance_strategy" FOREIGN KEY ("strategy_id")
          REFERENCES "Algo_Trading"."strategies"("id") ON DELETE CASCADE
      );
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP TABLE IF EXISTS "Algo_Trading"."strategy_performance";`);
    await queryRunner.query(`DROP TYPE IF EXISTS "Algo_Trading"."strategy_performance_status_enum";`);
  }
}
