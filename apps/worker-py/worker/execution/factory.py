"""Assembles the live execution stack (Phase 3c-3) with the risk gate's flatten wired to the
execution engine, so a daily-loss breach actually flattens positions.

This is also where TRADING_BRAIN is resolved. The brain is chosen ONCE, here, by returning a
different executor object — live_stream.py only ever calls `on_tick` / `on_final_candle` and
never learns which brain it got. Keeping the choice at construction time (rather than an `if`
inside LiveExecutor) is what guarantees the two brains cannot interleave: only one of them is
ever instantiated, so there is no path by which the strategy engine acts on a pattern-brain
position, or vice versa.
"""
from __future__ import annotations

import json
import logging

from ..config import config
from ..redis_bus import POSITIONS_UPDATE_CHANNEL, get_redis
from .binance_orders import BinanceOrderPlacer, FuturesOrderPlacer
from .execution import ExecutionService
from .execution_pg_store import PgExecutionStore
from .live_executor import LiveExecutor
from .pg_store import PgRiskGateStore
from .signals_pg_store import PgSignalStore

log = logging.getLogger("worker.execution.factory")

PATTERN_BRAIN = "pattern"
STRATEGY_BRAIN = "strategy"
KNOWN_BRAINS = (STRATEGY_BRAIN, PATTERN_BRAIN)


def build_live_executor(pool):
    """Return the executor for the configured TRADING_BRAIN.

    Returns LiveExecutor (strategy brain) or PatternExecutor (pattern brain); both expose
    `on_tick(ticker_id, close)` and `on_final_candle(ticker_id, close)`.
    """
    brain = config.trading_brain
    if brain not in KNOWN_BRAINS:
        # Fail loudly. A typo silently falling back to 'strategy' would quietly re-arm the
        # engine the operator believed they had switched off.
        raise ValueError(
            f"Unknown TRADING_BRAIN '{brain}' — must be one of: {', '.join(KNOWN_BRAINS)}."
        )

    exec_store = PgExecutionStore(pool)
    risk_store = PgRiskGateStore(pool)
    execution = ExecutionService(exec_store, risk_store, BinanceOrderPlacer(), FuturesOrderPlacer())
    # Close the risk-gate -> execution loop: on a breach the gate flattens via the engine.
    risk_store._flatten_fn = execution.flatten_all_positions_for_provider

    async def publish_positions() -> None:
        try:
            await get_redis().publish(POSITIONS_UPDATE_CHANNEL, json.dumps({"updated": True}))
        except Exception as err:  # noqa: BLE001
            log.warning("Failed to publish positions:update: %s", err)

    if brain == PATTERN_BRAIN:
        from ..pattern.executor import PatternExecutor

        log.warning("TRADING_BRAIN=pattern — the generated-strategy engine is NOT driving trading.")
        return PatternExecutor(execution, pool, publish_positions)

    from ..llm.service import LlmService

    return LiveExecutor(PgSignalStore(pool), execution, LlmService(), pool, publish_positions)
