import { MigrationInterface, QueryRunner } from 'typeorm';

export class InitialSchemaAndHypertable1700000000000 implements MigrationInterface {
  name = 'InitialSchemaAndHypertable1700000000000';

  public async up(queryRunner: QueryRunner): Promise<void> {
    // 1. Create Schema
    await queryRunner.query(`CREATE SCHEMA IF NOT EXISTS "Algo_Trading";`);

    // 2. Users Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."users" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "email" varchar NOT NULL UNIQUE,
        "password_hash" varchar NOT NULL,
        "created_at" TIMESTAMP NOT NULL DEFAULT now(),
        "updated_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 3. Providers Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."provider_type_enum" AS ENUM('binance', 'zerodha', 'mock');
      CREATE TYPE "Algo_Trading"."provider_status_enum" AS ENUM('active', 'inactive', 'error');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."providers" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "user_id" uuid NOT NULL REFERENCES "Algo_Trading"."users"("id") ON DELETE CASCADE,
        "name" varchar NOT NULL,
        "type" "Algo_Trading"."provider_type_enum" NOT NULL DEFAULT 'binance',
        "status" "Algo_Trading"."provider_status_enum" NOT NULL DEFAULT 'active',
        "trading_enabled" boolean NOT NULL DEFAULT true,
        "created_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 4. Provider Credentials Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."provider_credentials" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL UNIQUE REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "credential_json" jsonb NOT NULL,
        "created_at" TIMESTAMP NOT NULL DEFAULT now(),
        "updated_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 5. Provider Schedule Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."provider_schedule" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "active_days" text NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri,Sat,Sun',
        "active_start_time" varchar NOT NULL DEFAULT '00:00',
        "active_end_time" varchar NOT NULL DEFAULT '23:59',
        "timezone" varchar NOT NULL DEFAULT 'UTC'
      );
    `);

    // 6. Provider Balance Snapshots Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."provider_balance_snapshots" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "tradable_balance" numeric(18,8) NOT NULL,
        "currency" varchar NOT NULL DEFAULT 'USDT',
        "synced_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 7. Market Types Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."market_types" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "name" varchar NOT NULL
      );
    `);

    // 8. Tickers Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."ticker_status_enum" AS ENUM('active', 'inactive', 'onboarding');
      CREATE TYPE "Algo_Trading"."onboarding_stage_enum" AS ENUM('fetching_history', 'generating_strategy', 'backtesting', 'evaluating', 'ready', 'failed');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."tickers" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "market_type_id" uuid REFERENCES "Algo_Trading"."market_types"("id") ON DELETE SET NULL,
        "symbol" varchar NOT NULL,
        "interval" varchar NOT NULL DEFAULT '1m',
        "status" "Algo_Trading"."ticker_status_enum" NOT NULL DEFAULT 'onboarding',
        "onboarding_stage" "Algo_Trading"."onboarding_stage_enum" NOT NULL DEFAULT 'fetching_history',
        "created_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 9. OHLCV Data Table & TimescaleDB Hypertable
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."ohlcv_data" (
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "timestamp" TIMESTAMPTZ NOT NULL,
        "open" numeric(18,8) NOT NULL,
        "high" numeric(18,8) NOT NULL,
        "low" numeric(18,8) NOT NULL,
        "close" numeric(18,8) NOT NULL,
        "volume" numeric(18,8) NOT NULL,
        PRIMARY KEY ("ticker_id", "timestamp")
      );
    `);

    // Enable TimescaleDB Hypertable if timescaledb extension exists
    try {
      await queryRunner.query(`SELECT create_hypertable('"Algo_Trading"."ohlcv_data"', 'timestamp', if_not_exists => TRUE);`);
    } catch (e) {
      console.warn('TimescaleDB create_hypertable skipped or not supported on this DB:', e.message);
    }

    // 10. Data Quality Flags Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."flag_type_enum" AS ENUM('gap', 'duplicate', 'spike', 'tz_mismatch');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."data_quality_flags" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "flag_type" "Algo_Trading"."flag_type_enum" NOT NULL,
        "detail_json" jsonb,
        "detected_at" TIMESTAMP NOT NULL DEFAULT now(),
        "resolved_at" TIMESTAMPTZ
      );
    `);

    // 11. Strategies Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."strategy_status_enum" AS ENUM('draft', 'backtested', 'evaluated', 'live', 'retired');
      CREATE TYPE "Algo_Trading"."execution_mode_enum" AS ENUM('mode_a_rules', 'mode_b_ai_live');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."strategies" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "version" integer NOT NULL DEFAULT 1,
        "status" "Algo_Trading"."strategy_status_enum" NOT NULL DEFAULT 'draft',
        "execution_mode" "Algo_Trading"."execution_mode_enum" NOT NULL DEFAULT 'mode_a_rules',
        "parameters_json" jsonb NOT NULL,
        "generated_by" varchar NOT NULL DEFAULT 'claude-3-5-sonnet',
        "created_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 12. Backtest Results Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."backtest_results" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "strategy_id" uuid NOT NULL UNIQUE REFERENCES "Algo_Trading"."strategies"("id") ON DELETE CASCADE,
        "sharpe" numeric(10,4) NOT NULL,
        "sortino" numeric(10,4) NOT NULL,
        "calmar" numeric(10,4) NOT NULL,
        "max_drawdown" numeric(10,4) NOT NULL,
        "drawdown_duration" integer NOT NULL,
        "profit_factor" numeric(10,4) NOT NULL,
        "trade_count" integer NOT NULL,
        "monte_carlo_summary_json" jsonb,
        "regime_breakdown_json" jsonb,
        "passed_evaluation_gate" boolean NOT NULL DEFAULT false,
        "evaluated_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 13. Strategy Evaluation Policy Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."strategy_evaluation_policy" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "name" varchar NOT NULL DEFAULT 'Default Strict Policy',
        "min_sharpe" numeric(5,2) NOT NULL DEFAULT 1.0,
        "max_drawdown_pct" numeric(5,2) NOT NULL DEFAULT 20.0,
        "min_profit_factor" numeric(5,2) NOT NULL DEFAULT 1.3,
        "min_trade_count" integer NOT NULL DEFAULT 100,
        "max_parameter_count" integer NOT NULL DEFAULT 5,
        "is_active" boolean NOT NULL DEFAULT true
      );
    `);

    // Seed default strategy evaluation policy
    await queryRunner.query(`
      INSERT INTO "Algo_Trading"."strategy_evaluation_policy" ("name", "min_sharpe", "max_drawdown_pct", "min_profit_factor", "min_trade_count", "max_parameter_count", "is_active")
      VALUES ('Default Quantitative Thresholds Policy', 1.0, 20.0, 1.3, 100, 5, true);
    `);

    // 14. Live vs Backtest Divergence Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."live_vs_backtest_divergence" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "strategy_id" uuid NOT NULL REFERENCES "Algo_Trading"."strategies"("id") ON DELETE CASCADE,
        "measured_at" TIMESTAMP NOT NULL DEFAULT now(),
        "backtest_expected_metric" numeric(10,4) NOT NULL,
        "live_actual_metric" numeric(10,4) NOT NULL,
        "divergence_pct" numeric(5,2) NOT NULL,
        "flagged" boolean NOT NULL DEFAULT false
      );
    `);

    // 15. Allocation Snapshots Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."allocation_snapshots" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "allocated_capital" numeric(18,8) NOT NULL,
        "allocation_pct" numeric(5,2) NOT NULL,
        "computed_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);

    // 16. Risk Limits Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."reset_boundary_enum" AS ENUM('exchange_day', 'utc_midnight');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."risk_limits" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL UNIQUE REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "daily_loss_limit_pct" numeric(5,2) NOT NULL DEFAULT 2.0,
        "max_concurrent_positions_per_ticker" integer NOT NULL DEFAULT 1,
        "probation_size_pct" numeric(5,2) NOT NULL DEFAULT 25.0,
        "reset_boundary" "Algo_Trading"."reset_boundary_enum" NOT NULL DEFAULT 'utc_midnight'
      );
    `);

    // 17. Daily Loss Tracking Table
    await queryRunner.query(`
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."daily_loss_tracking" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "tracking_date" date NOT NULL,
        "capital_under_management_base" numeric(18,8) NOT NULL,
        "realized_pl" numeric(18,8) NOT NULL DEFAULT 0,
        "unrealized_pl" numeric(18,8) NOT NULL DEFAULT 0,
        "limit_breached" boolean NOT NULL DEFAULT false,
        "breached_at" TIMESTAMPTZ
      );
    `);

    // 18. Positions Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."position_status_enum" AS ENUM('open', 'closed');
      CREATE TYPE "Algo_Trading"."position_side_enum" AS ENUM('long', 'short');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."positions" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "ticker_id" uuid NOT NULL REFERENCES "Algo_Trading"."tickers"("id") ON DELETE CASCADE,
        "strategy_id" uuid REFERENCES "Algo_Trading"."strategies"("id") ON DELETE SET NULL,
        "side" "Algo_Trading"."position_side_enum" NOT NULL DEFAULT 'long',
        "status" "Algo_Trading"."position_status_enum" NOT NULL DEFAULT 'open',
        "entry_price" numeric(18,8) NOT NULL,
        "current_price" numeric(18,8),
        "exit_price" numeric(18,8),
        "quantity" numeric(18,8) NOT NULL,
        "unrealized_pl" numeric(18,8) NOT NULL DEFAULT 0,
        "realized_pl" numeric(18,8) NOT NULL DEFAULT 0,
        "is_probation" boolean NOT NULL DEFAULT true,
        "opened_at" TIMESTAMP NOT NULL DEFAULT now(),
        "closed_at" TIMESTAMPTZ
      );
    `);

    // 19. Orders Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."order_side_enum" AS ENUM('buy', 'sell');
      CREATE TYPE "Algo_Trading"."order_type_enum" AS ENUM('market', 'limit', 'stop_loss', 'take_profit');
      CREATE TYPE "Algo_Trading"."order_status_enum" AS ENUM('pending', 'filled', 'partial', 'rejected', 'cancelled');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."orders" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "position_id" uuid REFERENCES "Algo_Trading"."positions"("id") ON DELETE SET NULL,
        "provider_order_id" varchar,
        "side" "Algo_Trading"."order_side_enum" NOT NULL,
        "quantity" numeric(18,8) NOT NULL,
        "price" numeric(18,8),
        "order_type" "Algo_Trading"."order_type_enum" NOT NULL DEFAULT 'market',
        "status" "Algo_Trading"."order_status_enum" NOT NULL DEFAULT 'pending',
        "submitted_at" TIMESTAMP NOT NULL DEFAULT now(),
        "filled_at" TIMESTAMPTZ
      );
    `);

    // 20. Reconciliation Reports Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."reconciliation_status_enum" AS ENUM('clean', 'mismatch');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."reconciliation_reports" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "provider_id" uuid NOT NULL REFERENCES "Algo_Trading"."providers"("id") ON DELETE CASCADE,
        "run_at" TIMESTAMP NOT NULL DEFAULT now(),
        "mismatches_found" integer NOT NULL DEFAULT 0,
        "detail_json" jsonb,
        "status" "Algo_Trading"."reconciliation_status_enum" NOT NULL DEFAULT 'clean'
      );
    `);

    // 21. Alerts Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."alert_severity_enum" AS ENUM('critical', 'warning', 'info');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."alerts" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "severity" "Algo_Trading"."alert_severity_enum" NOT NULL DEFAULT 'info',
        "category" varchar NOT NULL,
        "message" text NOT NULL,
        "related_entity_type" varchar,
        "related_entity_id" varchar,
        "created_at" TIMESTAMP NOT NULL DEFAULT now(),
        "acknowledged_at" TIMESTAMPTZ
      );
    `);

    // 22. LLM Cost Log Table
    await queryRunner.query(`
      CREATE TYPE "Algo_Trading"."llm_purpose_enum" AS ENUM('strategy_generation', 're_evaluation', 'live_decision');
      CREATE TABLE IF NOT EXISTS "Algo_Trading"."llm_cost_log" (
        "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        "ticker_id" uuid,
        "strategy_id" uuid,
        "purpose" "Algo_Trading"."llm_purpose_enum" NOT NULL,
        "llm_provider" varchar NOT NULL DEFAULT 'direct_api',
        "model" varchar NOT NULL,
        "input_tokens" integer NOT NULL,
        "output_tokens" integer NOT NULL,
        "cost_usd" numeric(10,6) NOT NULL,
        "called_at" TIMESTAMP NOT NULL DEFAULT now()
      );
    `);
  }

  public async down(queryRunner: QueryRunner): Promise<void> {
    await queryRunner.query(`DROP SCHEMA IF EXISTS "Algo_Trading" CASCADE;`);
  }
}
