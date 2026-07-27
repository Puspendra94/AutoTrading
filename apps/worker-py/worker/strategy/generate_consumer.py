"""Consumer for `strategy:generate` jobs (consolidation Phase A — async generation UX).

The API (NestJS) publishes a generation request to Redis; this consumer runs the in-process
generator and publishes the result to `strategy:generated`, which the backend bridges to the
browser over Socket.IO. Only started when GENERATION_SOURCE=worker.
"""
from __future__ import annotations

import asyncio
import json
import logging

from ..db import get_pool
from ..redis_bus import STRATEGY_GENERATE_CHANNEL, STRATEGY_GENERATED_CHANNEL, get_redis

log = logging.getLogger("worker.strategy.generate")


async def _run_job(payload: dict) -> None:
    from ..llm.service import LlmService
    from .generator import StrategyGenerator
    from .pg_store import PgGeneratorStore

    request_id = payload.get("requestId")
    ticker_id = payload.get("tickerId")
    reason = payload.get("reason") or "manual generation (async)"
    interval = payload.get("interval") or None
    skip_gate = bool(payload.get("skipGate"))
    out = {"requestId": request_id, "tickerId": ticker_id}
    try:
        if not ticker_id:
            raise ValueError("missing tickerId")
        pool = await get_pool()
        generator = StrategyGenerator(PgGeneratorStore(pool), LlmService())
        result = await generator.generate(ticker_id, reason, interval_override=interval, skip_gate=skip_gate)
        out.update(
            saved=bool(result.get("saved")),
            attempts=result.get("attempts"),
            message=result.get("message"),
            evaluation=result.get("evaluation"),
            strategyId=result.get("strategyId"),
        )
    except Exception as err:  # noqa: BLE001 — never crash the consumer; report the failure to the UI
        log.exception("Generation job failed for ticker %s", ticker_id)
        out.update(saved=False, message=f"Generation failed: {err}")
    await get_redis().publish(STRATEGY_GENERATED_CHANNEL, json.dumps(out))
    log.info("Published generation result for request %s (saved=%s).", request_id, out.get("saved"))


async def run_generate_consumer(stop: asyncio.Event) -> None:
    """Subscribe to strategy:generate and process jobs sequentially until `stop` is set.
    Sequential on purpose: generation is heavy (LLM) and promotes a strategy, so we never want two
    running at once for the same ticker."""
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(STRATEGY_GENERATE_CHANNEL)
    log.info("Listening for generation jobs on '%s'.", STRATEGY_GENERATE_CHANNEL)
    try:
        while not stop.is_set():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue
            try:
                payload = json.loads(msg["data"])
            except Exception as err:  # noqa: BLE001
                log.warning("Bad strategy:generate payload: %s", err)
                continue
            await _run_job(payload)
    finally:
        await pubsub.aclose()
