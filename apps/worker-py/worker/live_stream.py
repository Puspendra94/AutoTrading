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
from .redis_bus import MARKET_TICK_CHANNEL, get_redis

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
            "SELECT id, symbol, interval FROM tickers WHERE status = ANY($1::text[])",
            list(_STREAMABLE_STATUSES),
        )
    return [dict(r) for r in rows]


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


async def _handle_message(raw: str, by_key: dict[str, str]) -> None:
    """Parse one combined-stream frame, publish the tick, persist on close."""
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
    tick = {
        "tickerId": ticker_id,
        "symbol": symbol.upper(),
        "interval": k["i"],
        "openTime": int(k["t"]),
        "closeTime": int(k["T"]),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "isFinal": is_final,
    }

    # Publish every tick (chart wants intra-candle updates); persist only closes.
    await get_redis().publish(MARKET_TICK_CHANNEL, json.dumps(tick))
    if is_final:
        await _upsert_final_candle(ticker_id, k)


async def run_live_stream(stop: asyncio.Event) -> None:
    """Own the live kline stream until `stop` is set, reconnecting on failure."""
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
                        await _handle_message(raw, by_key)
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
