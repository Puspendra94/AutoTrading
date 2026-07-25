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

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


config = Config()
