"""Shared async Redis client + the platform's pub/sub channel contract.

The worker publishes domain events (live ticks, later positions/regeneration) to Redis; the
NestJS backend subscribes and fans them out to browsers over Socket.IO. This is the async,
cross-process communication layer that lets the worker own data/AI without the backend
having to be up — see MIGRATION.md for the full channel table.
"""
from __future__ import annotations

import logging

import redis.asyncio as redis

from .config import config

log = logging.getLogger("worker.redis")

# Channel names — must match the backend's constants.
MARKET_TICK_CHANNEL = "market:tick"          # market-stream.service.ts
JOBS_TRIGGER_CHANNEL = "jobs:trigger"        # internal-jobs-redis.consumer.ts
STRATEGY_REGENERATE_CHANNEL = "strategy:regenerate"  # strategy-regenerate.consumer.ts
POSITIONS_UPDATE_CHANNEL = "positions:update"  # market-stream.service.ts (worker owns execution)
STRATEGY_GENERATE_CHANNEL = "strategy:generate"      # API -> worker: run a generation job
STRATEGY_GENERATED_CHANNEL = "strategy:generated"    # worker -> API: generation result (bridged to WS)
SIGNALS_REQUEST_CHANNEL = "signals:request"          # API -> worker: compute chart markers
SIGNALS_RESPONSE_CHANNEL = "signals:response"        # worker -> API: chart markers (req/resp by requestId)
SIGNALS_UPDATE_CHANNEL = "signals:update"            # worker -> API: a bar closed; refresh markers (bridged to WS)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Lazily create a single shared async Redis client (publish-only for now)."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=config.redis_host,
            port=config.redis_port,
            # None (not "") when unset: redis-py sends an AUTH command for any non-None value,
            # and an empty-string password fails against a server with no requirepass.
            password=config.redis_password or None,
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
