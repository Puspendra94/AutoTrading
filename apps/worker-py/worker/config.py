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

    # --- Market data venue: USD-M FUTURES, not spot.
    #
    # This has to match the venue orders are placed on. Deciding on spot prices while filling on
    # futures leaves the two separated by the basis, so what the chart shows and what a live fill
    # costs are quietly different numbers — the kind of gap that only surfaces with real money.
    #
    # data.binance.vision public archive (no auth). Monthly/daily kline zips.
    binance_vision_base: str = os.getenv("BINANCE_VISION_BASE", "https://data.binance.vision")
    # fapi, not api: the futures REST host. Note the PATH also differs (/fapi/v1 vs /api/v3), so
    # this host is only interchangeable with a client that knows it is talking to futures.
    binance_api_base: str = os.getenv("BINANCE_API_BASE", "https://fapi.binance.com")
    # Live kline WebSocket (public, unauthenticated).
    #
    # The `/market` segment is REQUIRED and is not decoration. Binance's 2026-03-06 futures
    # WebSocket upgrade split the endpoint into three routed paths — /public (high-frequency
    # data), /market (regular market data, which is where klines live) and /private (user data)
    # — and retired the legacy wss://fstream.binance.com/ws and /stream on 2026-04-23.
    #
    # The failure mode is vicious: the legacy URL still completes the TLS handshake, still
    # returns a valid 101 upgrade, and still ACKs a SUBSCRIBE with {"result":null} — it simply
    # never pushes a frame. Nothing errors, nothing reconnects, and the engine just stops being
    # driven. Verified directly: /market/stream and /market/ws deliver klines; the bare /stream
    # and /public/stream do not.
    binance_ws_base: str = os.getenv("BINANCE_WS_BASE", "wss://fstream.binance.com/market")
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
    # 2020-01, not 2017-08: the USD-M futures archive begins in January 2020 (2019-12 returns 404).
    # Spot goes back to 2017-08, but that history is a different instrument and must not be mixed in.
    backfill_start: str = os.getenv("BACKFILL_START", "2020-01")
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
    # Groq. Note the token budget: ENTRY_MAX_TOKENS is 12288 because DeepSeek's reasoning models
    # spend ~5k tokens thinking before they emit any JSON. A non-reasoning model on Groq
    # (llama-3.3-70b-versatile and friends) needs a fraction of that, and giving it 12288 only
    # widens the window for a runaway generation — see LLM_MODELS guidance in the README.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
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

    # --- Which brain drives live trading. ---------------------------------------------------
    # 'strategy' (default) = the generated-strategy engine: backtested + gate-promoted strategies,
    #     evaluated on candle close by the DSL interpreter. Everything below this line in the
    #     strategy/ package belongs to it.
    # 'pattern'  = the pattern/indicator brain: a deterministic feature engine (TA-Lib + chart
    #     patterns + S/R levels) decides WHEN to ask the LLM, and the LLM returns a concrete
    #     entry/exit decision that local guardrails then validate and size.
    #
    # The two are mutually exclusive and must never run together — they would both drive
    # on_final_candle, both enforce exits over the same open position, and both write chart
    # markers. Selecting 'pattern' therefore also disables strategy re-evaluation, strategy
    # generation and the chart-marker replay consumer (see main.py), so nothing can promote a
    # strategy to live, or repaint the chart, behind the pattern brain's back.
    #
    # Nothing is deleted: flip back to 'strategy' and restart to get the old behaviour verbatim.
    trading_brain: str = os.getenv("TRADING_BRAIN", "strategy").strip().lower()

    # --- Pattern brain (TRADING_BRAIN=pattern). -----------------------------------------------
    # The timeframe the feature engine evaluates on. 15m is the deliberate default: BTC's 5m ATR
    # is small enough that the noise dominates (measured on real data, and the strategy-brain
    # backtests said the same), while 15m still produces several triggers a day to watch.
    pattern_interval: str = os.getenv("PATTERN_INTERVAL", "15m")
    # Bars loaded per evaluation. Must comfortably exceed the indicator warm-up (EMA200 + margin
    # = 260) or the engine never goes warm and never emits a trigger.
    pattern_candle_limit: int = int(os.getenv("PATTERN_CANDLE_LIMIT", "500"))
    # The decision loop (gate -> LLM -> validate -> size -> order). OFF by default even under
    # TRADING_BRAIN=pattern, so the feature engine can be watched producing triggers for as long
    # as you like before anything is allowed to spend money or open a position. Turning this on
    # is the deliberate step from "observing" to "trading".
    pattern_decisions_enabled: bool = os.getenv("PATTERN_DECISIONS_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    # Minimum reward:risk an entry must offer against its sized stop.
    #
    # Lowered from 1.5 to 1.2 because it had become the binding constraint on trading at all.
    # Stop distance is floored at MIN_STOP_ATR (3.0) and the target is R:R multiples of THAT, so
    # 1.5 demanded a level 4.5 ATR away; in a range there frequently is none, and the model was
    # correctly refusing. Measured on the host: 17 of 22 model skips cited the stop or the R:R.
    # At 1.2 the target sits 3.6 ATR out — a 20% smaller move to find.
    #
    # This costs less than it looks, because the target is a HANDOFF, not an exit: crossing it
    # ratchets the stop to breakeven+fees and starts the ATR trail rather than closing. A lower
    # minimum therefore starts the runner sooner, it does not cap the winner.
    #
    # Env-tunable on purpose — it is being actively tuned, and a redeploy per experiment is waste.
    min_risk_reward: float = float(os.getenv("MIN_RISK_REWARD", "1.2"))

    # --- Trading costs. ------------------------------------------------------------------------
    # These are charged on paper fills as well as live ones, and that is the entire point.
    #
    # A paper record that books (exit - entry) * qty and nothing else is not a cheaper version of
    # live trading, it is a different game. Measured over the first paper week: 41 trades, $229k of
    # cumulative notional, -$254 booked — and $184 of taker fees that were never charged. The real
    # result was -$438, so 42% of the loss was invisible, and every downstream consumer (the daily
    # risk budget, the AI lessons loop, the supervisor's profit factor) was reading the flattering
    # number.
    #
    # Binance USD-M taker is 0.04%/side, spot taker 0.10%/side. Both are the pre-discount public
    # rates; lower them here if a BNB/VIP discount applies.
    futures_taker_fee_pct: float = float(os.getenv("FUTURES_TAKER_FEE_PCT", "0.0004"))
    spot_taker_fee_pct: float = float(os.getenv("SPOT_TAKER_FEE_PCT", "0.001"))
    # Adverse fill assumed on a SIMULATED market order, as a fraction of price. Live fills need no
    # estimate — the exchange reports what was actually paid. 0.02% is deliberately conservative
    # for BTCUSDT top-of-book; widen it for thinner symbols.
    paper_slippage_pct: float = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0002"))

    # Phase 4b: live SHORT execution on the futures venue. Off by default — a short strategy stays
    # flat (HOLD) until this is explicitly enabled, so shorting can never happen by surprise. When
    # on, a live short strategy on a futures ticker opens a real SELL-to-open (or a simulated fill
    # while the provider is in paper mode). Long execution is unaffected (a 1x futures long == spot).
    futures_execution_enabled: bool = os.getenv("FUTURES_EXECUTION_ENABLED", "false").lower() in ("1", "true", "yes", "on")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


config = Config()
