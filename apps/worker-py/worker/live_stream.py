"""Live Binance kline ingestion — the worker-owned replacement for the backend's
in-process MarketStreamService (Phase 1, see MIGRATION.md).

Responsibilities:
  * Open a single combined Binance kline WebSocket for every streamable ticker.
  * Publish EVERY tick (intra-candle and final) to Redis `market:tick` so the backend can
    fan it out to browser charts and run its evaluate/execute path unchanged.
  * Upsert only FINAL candles into `ohlcv_data` — the worker is now the writer of record,
    so ingestion continues even if the backend is down (problem 3).

Idempotent and self-healing: reconnects with backoff, and final-candle upserts use
ON CONFLICT so a re-received candle (or an overlap with archive backfill) is harmless.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from .config import config
from .db import get_pool
from .redis_bus import MARKET_TICK_CHANNEL, SIGNALS_UPDATE_CHANNEL, get_redis

log = logging.getLogger("worker.live")

# Statuses whose tickers are worth streaming — everything the backend streamed too
# (ACTIVE = live-strategy-managed, ONBOARDING = added but not yet ready). INACTIVE means
# backfill failed; nothing to stream.
_STREAMABLE_STATUSES = ("active", "onboarding")

_RECONNECT_DELAY_S = 5


async def _load_streamable_tickers() -> list[dict]:
    """Return [{id, symbol, interval}] for every streamable ticker."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # status is a Postgres enum (ticker_status_enum); cast to text so it compares to the
            # text[] param without needing the enum type name here.
            "SELECT id, symbol, interval FROM tickers WHERE status::text = ANY($1::text[])",
            list(_STREAMABLE_STATUSES),
        )
    # id -> str: it flows into the JSON tick payload (must be serializable) and every downstream
    # store query, which expect a string ticker id (asyncpg returns a UUID object here).
    return [{"id": str(r["id"]), "symbol": r["symbol"], "interval": r["interval"]} for r in rows]


async def _upsert_final_candle(ticker_id: str, k: dict) -> None:
    """Persist a closed candle. UPDATE-on-conflict so a corrected re-send wins; matches the
    (ticker_id, timestamp) unique key the archive backfill and backend upsert both use."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ohlcv_data (ticker_id, timestamp, open, high, low, close, volume)
            VALUES ($1, to_timestamp($2 / 1000.0), $3, $4, $5, $6, $7)
            ON CONFLICT (ticker_id, timestamp) DO UPDATE
              SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                  close = EXCLUDED.close, volume = EXCLUDED.volume
            """,
            ticker_id,
            int(k["t"]),
            float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]),
        )


def _stream_url(tickers: list[dict]) -> str:
    """Combined-stream URL: one socket for all <symbol>@kline_<interval> streams."""
    streams = "/".join(f"{t['symbol'].lower()}@kline_{t['interval']}" for t in tickers)
    return f"{config.binance_ws_base}/stream?streams={streams}"


async def _handle_message(raw: str, by_key: dict[str, str], executor=None) -> None:
    """Parse one combined-stream frame, publish the tick, persist on close, and — when the
    worker owns execution — drive the live trading loop."""
    msg = json.loads(raw)
    data = msg.get("data", msg)  # combined streams wrap payload under "data"
    k = data.get("k")
    if not k:
        return

    symbol = data.get("s") or msg.get("s")
    key = f"{symbol.upper()}@{k['i']}"
    ticker_id = by_key.get(key)
    if not ticker_id:
        return  # a stream we didn't ask for / ticker vanished

    is_final = bool(k["x"])
    close = float(k["c"])
    tick = {
        "tickerId": ticker_id,
        "symbol": symbol.upper(),
        "interval": k["i"],
        "openTime": int(k["t"]),
        "closeTime": int(k["T"]),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": close,
        "volume": float(k["v"]),
        "isFinal": is_final,
    }

    # Publish every tick (chart wants intra-candle updates); persist only closes.
    await get_redis().publish(MARKET_TICK_CHANNEL, json.dumps(tick))
    if is_final:
        await _upsert_final_candle(ticker_id, k)
        # A bar closed, so the strategy's entry/exit markers may have changed. Tell the browsers to
        # refresh them — the same push-then-render path live candles already use, so intents appear
        # without a reload (and for shorts/exits too, since the marker set is direction-aware).
        await get_redis().publish(SIGNALS_UPDATE_CHANNEL, json.dumps({"tickerId": ticker_id}))

    # Live trading loop (Phase 3c-3) — mark price + hard exits every tick, evaluate+execute on
    # close. Only when the worker owns execution; a failure must not break ingestion.
    if executor is not None:
        try:
            await executor.on_tick(ticker_id, close)
            if is_final:
                await executor.on_final_candle(ticker_id, close)
        except Exception:  # noqa: BLE001
            log.exception("Live execution failed for ticker %s", ticker_id)


async def run_live_stream(stop: asyncio.Event) -> None:
    """Own the live kline stream until `stop` is set, reconnecting on failure."""
    executor = None
    if config.worker_owns_execution:
        from .execution.factory import build_live_executor

        executor = build_live_executor(await get_pool())
        log.warning("WORKER OWNS EXECUTION — live orders will be driven from this process. "
                    "Ensure the backend is set to LIVE_EXECUTION_SOURCE=worker.")

    while not stop.is_set():
        tickers = await _load_streamable_tickers()
        if not tickers:
            log.info("No streamable tickers yet; retrying in %ss.", _RECONNECT_DELAY_S)
            await _sleep_or_stop(stop, _RECONNECT_DELAY_S)
            continue

        by_key = {f"{t['symbol'].upper()}@{t['interval']}": t["id"] for t in tickers}
        url = _stream_url(tickers)
        log.info("Opening live kline stream for %d ticker(s): %s",
                 len(tickers), ", ".join(by_key.keys()))
        try:
            async with websockets.connect(url, ping_interval=180, ping_timeout=600) as ws:
                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        await _handle_message(raw, by_key, executor)
                    except Exception:  # noqa: BLE001 — one bad frame must not kill the stream
                        log.exception("Failed to handle kline frame")
        except Exception as err:  # noqa: BLE001 — reconnect on any socket error
            log.warning("Live stream disconnected (%s); reconnecting in %ss.",
                        err, _RECONNECT_DELAY_S)
        await _sleep_or_stop(stop, _RECONNECT_DELAY_S)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if stop is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
