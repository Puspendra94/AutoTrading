"""Assembles the live execution stack (Phase 3c-3) with the risk gate's flatten wired to the
execution engine, so a daily-loss breach actually flattens positions."""
from __future__ import annotations

import json
import logging

from ..redis_bus import POSITIONS_UPDATE_CHANNEL, get_redis
from .binance_orders import BinanceOrderPlacer
from .execution import ExecutionService
from .execution_pg_store import PgExecutionStore
from .live_executor import LiveExecutor
from .pg_store import PgRiskGateStore
from .signals_pg_store import PgSignalStore

log = logging.getLogger("worker.execution.factory")


def build_live_executor(pool) -> LiveExecutor:
    from ..llm.service import LlmService

    exec_store = PgExecutionStore(pool)
    risk_store = PgRiskGateStore(pool)
    execution = ExecutionService(exec_store, risk_store, BinanceOrderPlacer())
    # Close the risk-gate -> execution loop: on a breach the gate flattens via the engine.
    risk_store._flatten_fn = execution.flatten_all_positions_for_provider

    async def publish_positions() -> None:
        try:
            await get_redis().publish(POSITIONS_UPDATE_CHANNEL, json.dumps({"updated": True}))
        except Exception as err:  # noqa: BLE001
            log.warning("Failed to publish positions:update: %s", err)

    return LiveExecutor(PgSignalStore(pool), execution, LlmService(), pool, publish_positions)
