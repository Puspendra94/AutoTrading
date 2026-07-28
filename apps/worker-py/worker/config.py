"""Runtime configuration, read from the same env vars the NestJS backend uses so both
processes point at one Postgres/Timescale instance and the `Algo_Trading` schema."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    db_host: str = os.getenv("DATABASE_HOST", "localhost")
    db_port: int = int(os.getenv("DATABASE_PORT", "5433"))
    db_user: str = os.getenv("DATABASE_USER", "postgres")
    db_password: str = os.getenv("DATABASE_PASSWORD", "postgres")
    db_name: str = os.getenv("DATABASE_NAME", "postgres")
    db_schema: str = os.getenv("DATABASE_SCHEMA", "Algo_Trading")

    redis_host: str = os.getenv("REDIS_HOST", "localhost")
    redis_port: int = int(os.getenv("REDIS_PORT", "55000"))

    # Backend API — the worker triggers the (former Node-worker) jobs via its guarded
    # /internal/jobs/* endpoints. In Docker this is the compose service name; locally it's
    # the dev backend. INTERNAL_API_KEY is the shared secret those endpoints require.
    backend_url: str = os.getenv("BACKEND_URL", "http://localhost:3009")
    internal_api_key: str = os.getenv("INTERNAL_API_KEY", "")
    balance_sync_interval_minutes: int = int(os.getenv("BALANCE_SYNC_INTERVAL_MINUTES", "5"))
    # How scheduled jobs reach the backend (Phase 2, see MIGRATION.md):
    #   http  (default) = POST /internal/jobs/* synchronously (worker depends on backend).
    #   redis           = publish the job name to jobs:trigger; backend consumes it async,
    #                     so the worker no longer blocks on or depends on the backend.
    job_dispatch: str = os.getenv("JOB_DISPATCH", "http").lower()

    # data.binance.vision public archive (no auth). Spot monthly/daily kline zips.
    binance_vision_base: str = os.getenv("BINANCE_VISION_BASE", "https://data.binance.vision")
    binance_api_base: str = os.getenv("BINANCE_API_BASE", "https://api.binance.com")
    # Live kline WebSocket (public, unauthenticated). Phase 1: the worker owns this stream,
    # writes final candles to ohlcv_data, and publishes every tick to Redis for the backend.
    binance_ws_base: str = os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443")
    # Master switch for the live-ingestion pipeline. Keep this OFF while the backend still
    # streams in-process (LIVE_STREAM_SOURCE=internal) so the same candle isn't ingested
    # twice; flip both together at cutover (worker on, backend -> redis).
    live_stream_enabled: bool = os.getenv("LIVE_STREAM_ENABLED", "false").lower() in ("1", "true", "yes", "on")

    # Single base interval = the minimum Binance publishes (1m). Everything else is a
    # DB-level roll-up via TimescaleDB time_bucket (see worker/candles.py), so we store
    # one source of truth and derive 5m/1h/1d/etc. on demand — cheap, and no duplicate
    # ingestion. Overridable, but 1m is the intended default.
    backfill_symbol: str = os.getenv("BACKFILL_SYMBOL", "BTCUSDT")
    base_interval: str = os.getenv("BASE_INTERVAL", "1m")
    backfill_intervals: tuple[str, ...] = tuple(
        i.strip() for i in os.getenv("BACKFILL_INTERVALS", os.getenv("BASE_INTERVAL", "1m")).split(",") if i.strip()
    )
    # Binance spot BTCUSDT history starts 2017-08. Override to shrink for testing.
    backfill_start: str = os.getenv("BACKFILL_START", "2017-08")
    # Run a gap-fill on every startup (cheap — resumes from the last stored month).
    backfill_on_start: bool = os.getenv("BACKFILL_ON_START", "true").lower() in ("1", "true", "yes", "on")

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Consecutive provider-API failures before the execution store trips the kill switch —
    # matches the backend's PROVIDER_API_FAILURE_THRESHOLD.
    provider_api_failure_threshold: int = int(os.getenv("PROVIDER_API_FAILURE_THRESHOLD", "5"))

    # --- LLM (provider-agnostic fallback chain — mirrors the backend's LLM_MODELS). ---
    llm_models: str = os.getenv("LLM_MODELS") or "direct_api:claude-opus-4-8"
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    # Only needed for 'bedrock' entries in LLM_MODELS.
    aws_region: str = os.getenv("AWS_REGION", "")
    aws_access_key_id: str = os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret_access_key: str = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    aws_session_token: str = os.getenv("AWS_SESSION_TOKEN", "")
    aws_bedrock_api_key: str = os.getenv("AWS_BEDROCK_API_KEY", "")

    # --- Strategy Supervisor (Phase 3a) — deterministic, no-AI health monitor that watches
    # live strategies against guardrails and, on a trip, asks the backend to regenerate.
    # Off by default (dark launch); when on, the daily strategy-reevaluation job is redundant.
    supervisor_enabled: bool = os.getenv("SUPERVISOR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    supervisor_interval_minutes: int = int(os.getenv("SUPERVISOR_INTERVAL_MINUTES", "5"))
    supervisor_min_closed_trades: int = int(os.getenv("SUPERVISOR_MIN_CLOSED_TRADES", "5"))
    # Live-vs-backtest profit-factor divergence %, matching the backend's threshold (spec 7.5).
    supervisor_divergence_pct: float = float(os.getenv("SUPERVISOR_DIVERGENCE_PCT", "30"))
    supervisor_max_consecutive_losses: int = int(os.getenv("SUPERVISOR_MAX_CONSECUTIVE_LOSSES", "5"))
    supervisor_max_drawdown_pct: float = float(os.getenv("SUPERVISOR_MAX_DRAWDOWN_PCT", "20"))
    supervisor_min_win_rate_pct: float = float(os.getenv("SUPERVISOR_MIN_WIN_RATE_PCT", "30"))
    # Notional account base drawdown is measured against — matches StrategyPerformance's
    # notionalUsd default, so a small PnL dip isn't misread as a huge % drawdown.
    supervisor_notional_base: float = float(os.getenv("SUPERVISOR_NOTIONAL_BASE", "10000"))
    # When a guardrail trips: False (default) publishes strategy:regenerate for the backend to
    # run generateStrategyForTicker; True runs the ported generation in-process (Phase 3b-3).
    # Keep False until the in-process generator has been validated end-to-end.
    supervisor_generate_inline: bool = os.getenv("SUPERVISOR_GENERATE_INLINE", "false").lower() in ("1", "true", "yes", "on")

    # Worker owns the live trading loop (Phase 3c-3): mark-price, hard exits, and — on candle
    # close — evaluate signal + execute. Off by default; when ON, the backend MUST be set to
    # LIVE_EXECUTION_SOURCE=worker so execution never runs in both places (double orders).
    # Requires LIVE_STREAM_ENABLED (the worker must own the stream to drive execution).
    worker_owns_execution: bool = os.getenv("WORKER_OWNS_EXECUTION", "false").lower() in ("1", "true", "yes", "on")

    # Default timeframe a live strategy is evaluated on when its own eval_interval is unset —
    # mirrors the backend's STRATEGY_EVAL_INTERVAL (default 1h). The live-signal path aggregates
    # the 1m base to this interval so what trades live matches what was backtested/promoted.
    strategy_eval_interval: str = os.getenv("STRATEGY_EVAL_INTERVAL", "1h")
    # Default number of aggregated bars the planner backtests over (mirrors backend evalCandleLimit).
    strategy_eval_candle_limit: int = int(os.getenv("STRATEGY_EVAL_CANDLE_LIMIT", "10000"))

    # Phase 2 hybrid AI exit overlay: on a rules-mode (Mode A) strategy, when the deterministic
    # ladder says HOLD on a WINNING open position, consult the LLM on whether the move looks
    # exhausted and the profit should be booked early. The AI can only TIGHTEN (force an early
    # exit), never veto a rules exit or open a position. Off by default — it costs one LLM call per
    # in-profit candle. Mirrors the backend's HYBRID_EXIT_AI.
    hybrid_exit_ai: bool = os.getenv("HYBRID_EXIT_AI", "false").lower() in ("1", "true", "yes", "on")

    # Consolidation Phase A: when 'worker', the worker consumes strategy:generate jobs from the API
    # and runs strategy generation in-process (result pushed back over the websocket). 'backend'
    # (default) leaves generation in the NestJS backend. Mirrors the backend's GENERATION_SOURCE.
    generation_source: str = os.getenv("GENERATION_SOURCE", "backend")

    # Two-stage generation: the LLM proposes a strategy SHAPE (template with a `search` grid) and a
    # deterministic optimizer finds the best parameters on the data (walk-forward), instead of the
    # LLM guessing concrete numbers. Off by default; opt-in while it's validated.
    two_stage_generation: bool = os.getenv("TWO_STAGE_GENERATION", "false").lower() in ("1", "true", "yes", "on")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


config = Config()
