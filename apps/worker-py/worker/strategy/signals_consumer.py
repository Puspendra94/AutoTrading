"""Consumer for `signals:request` (consolidation Phase B) — computes a live strategy's buy/sell
chart markers in the worker and publishes them back to the API over `signals:response`, so the
backend no longer needs the DSL interpreter for display. Request/response is correlated by requestId.
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..db import get_pool
from ..execution.signals_pg_store import PgSignalStore
from ..redis_bus import SIGNALS_REQUEST_CHANNEL, SIGNALS_RESPONSE_CHANNEL, get_redis
from .dsl.interpreter import generate_signals_from_ir
from .dsl.ir import resolve_ir

log = logging.getLogger("worker.strategy.signals")


async def _compute(payload: dict) -> dict:
    request_id = payload.get("requestId")
    ticker_id = payload.get("tickerId")
    interval = payload.get("interval") or "15m"
    limit = int(payload.get("limit") or 500)
    out = {"requestId": request_id, "strategyId": None, "strategyName": None, "signals": []}
    try:
        store = PgSignalStore(await get_pool())
        live = await store.get_live_strategy(ticker_id)
        if not live:
            return out
        ir = resolve_ir(live["parametersJson"])
        if not ir:
            return {**out, "strategyId": live["id"]}
        # Markers are drawn on the chart's CURRENT display interval (may differ from eval interval),
        # replayed over the same interval-rolled candles the chart renders so they line up.
        candles = await store.load_recent_candles_for_interval(ticker_id, interval, limit)
        signals = generate_signals_from_ir(candles, ir)
        name = live["parametersJson"].get("strategyName") or "Strategy"
        # direction lets the chart label a short strategy's markers as SHORT/COVER (a short opens
        # with a sell), instead of the raw buy/sell that only reads right for a long.
        direction = ir.get("direction") or "long"
        return {"requestId": request_id, "strategyId": live["id"], "strategyName": name,
                "direction": direction, "signals": signals}
    except Exception as err:  # noqa: BLE001 — never crash the consumer; return empty markers
        log.warning("Signals computation failed for ticker %s: %s", ticker_id, err)
        return out


async def run_signals_consumer(stop: asyncio.Event) -> None:
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(SIGNALS_REQUEST_CHANNEL)
    log.info("Listening for chart-signal requests on '%s'.", SIGNALS_REQUEST_CHANNEL)
    try:
        while not stop.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue
            try:
                payload = json.loads(msg["data"])
            except Exception as err:  # noqa: BLE001
                log.warning("Bad signals:request payload: %s", err)
                continue
            result = await _compute(payload)
            await get_redis().publish(SIGNALS_RESPONSE_CHANNEL, json.dumps(result))
    finally:
        await pubsub.aclose()
