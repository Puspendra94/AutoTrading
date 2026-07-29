"""Live loop for the pattern brain — the TRADING_BRAIN='pattern' counterpart of
execution/live_executor.py::LiveExecutor.

Duck-typed against LiveExecutor (`on_tick` / `on_final_candle`) so live_stream.py drives
whichever brain the factory built without knowing which one it got.

PHASE 1: the feature engine runs and its triggers are persisted as chart markers. It still
places no orders, enforces no exits and calls no LLM — the only side effects are a
`pattern_signals` row and a mark-price update.

The stream fires on every 1m close, but the engine evaluates on `config.pattern_interval`
(15m). Rather than counting ticks, each 1m close asks the DB for the rolled-up bars and
processes only when a NEW COMPLETE bar has appeared. That is naturally correct across restarts,
reconnects and replayed closes, none of which a counter would survive.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

from ..candles import get_candles
from ..config import config
from ..util import to_fixed
from .features import build_feature_state
from .store import PatternSignalStore, interval_ms

log = logging.getLogger("worker.pattern.executor")

LONG = "long"


class PatternExecutor:
    def __init__(
        self,
        execution: Any,
        pool: Any,
        publish_positions: Optional[Callable[[], Awaitable[None]]] = None,
        store: Optional[PatternSignalStore] = None,
        load_candles: Optional[Callable[..., Awaitable[list[dict]]]] = None,
    ) -> None:
        self.execution = execution
        self.pool = pool
        self.publish_positions = publish_positions
        self.store = store if store is not None else (PatternSignalStore(pool) if pool else None)
        # Injectable so the engine can be driven from a test or a historical replay without a DB.
        self.load_candles = load_candles or get_candles
        self.interval = config.pattern_interval
        self.candle_limit = config.pattern_candle_limit
        self._last_bar_ms: dict[str, int] = {}
        self._warned_cold = False

    async def on_tick(self, ticker_id: str, close: float) -> None:
        # Mark-to-market only. Deliberately NOT calling execution.enforce_hard_exits: those are
        # the provider-level hard caps the strategy brain relies on, and letting them run here
        # would mean two exit systems acting on one position the moment the pattern brain opens
        # anything. The pattern brain's own exit ladder lands in Phase 3.
        await self._update_mark_prices(ticker_id, close)

    async def on_final_candle(self, ticker_id: str, close: float) -> None:
        """Evaluate features when a new COMPLETE bar of the decision interval has closed."""
        state = await self.evaluate(ticker_id)
        if state is None:
            return

        if state.triggers:
            log.info(
                "Pattern triggers on %s %s bar %d: %s | regime=%s",
                ticker_id, self.interval, state.bar_time_ms,
                ", ".join(f"{t['name']}/{t['side']}" for t in state.triggers),
                state.regime["label"],
            )
            if self.store is not None:
                await self.store.save_triggers(
                    ticker_id, self.interval, state.bar_time_ms,
                    state.triggers, state.to_state_pack(),
                )

    async def evaluate(self, ticker_id: str):
        """Load candles, drop the in-progress bar, and build the feature state for the newest
        complete one. Returns None when there is nothing new to evaluate.

        Split out from on_final_candle so it can be driven directly — by a test, or by a
        backfill that replays history through the same engine.
        """
        candles = await self.load_candles(ticker_id, self.interval, self.candle_limit)
        candles = _drop_incomplete_bar(candles, self.interval)
        if not candles:
            return None

        bar_ms = _bar_time_ms(candles[-1])
        if self._last_bar_ms.get(ticker_id) == bar_ms:
            return None  # this bar has already been evaluated
        self._last_bar_ms[ticker_id] = bar_ms

        symbol = await self._symbol_for(ticker_id)
        state = build_feature_state(ticker_id, symbol, self.interval, candles)

        if not state.warm:
            if not self._warned_cold:
                # Once per process: a cold engine is normal right after a backfill, but if it
                # stays cold it means the history is too short and NOTHING will ever trigger.
                log.warning(
                    "Feature engine is COLD for %s — %d %s bars loaded, indicators not warm yet. "
                    "No triggers will be emitted until enough history exists.",
                    ticker_id, len(candles), self.interval,
                )
                self._warned_cold = True
            return state

        return state

    async def _symbol_for(self, ticker_id: str) -> str:
        if self.pool is None:
            return ""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT symbol FROM tickers WHERE id = $1", ticker_id)
        return row["symbol"] if row else ""

    async def _update_mark_prices(self, ticker_id: str, close: float) -> None:
        positions = await self.execution.store.find_open_positions_by_ticker(ticker_id)
        if not positions:
            return
        for p in positions:
            entry = float(p["entryPrice"])
            qty = float(p["quantity"])
            unrealized = (close - entry) * qty if p["side"] == LONG else (entry - close) * qty
            await self.execution.store.update_position_mark(p["id"], close, to_fixed(unrealized, 8))
        if self.publish_positions:
            await self.publish_positions()


def _bar_time_ms(candle: dict) -> int:
    ts = candle["timestamp"]
    return int(ts.timestamp() * 1000) if hasattr(ts, "timestamp") else int(ts)


def _drop_incomplete_bar(candles: list[dict], interval: str, now_ms: Optional[int] = None) -> list[dict]:
    """Remove the trailing bar if its period has not elapsed yet.

    `time_bucket` happily returns the bucket currently being built, so the newest row is a
    PARTIAL candle whose high/low/close are still moving. Feeding that to the engine would let
    an indicator — and a trigger — flip back and forth within a single bar.
    """
    if not candles:
        return candles
    step = interval_ms(interval)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if _bar_time_ms(candles[-1]) + step > now:
        return candles[:-1]
    return candles
