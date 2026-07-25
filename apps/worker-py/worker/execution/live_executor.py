"""Worker-side live trading loop — the port of MarketStreamService's per-tick execution
responsibilities (Phase 3c-3).

on_tick (every tick): refresh open-position mark prices + enforce hard stop-loss/take-profit.
on_final_candle (candle close): evaluate the live signal and execute — BUY opens via the risk
gate, SELL closes the matching open position, HOLD does nothing. Mirrors handleTick +
evaluateAndExecute exactly (mark price, then hard exits, then — on close — evaluate/execute).

Wired into the live stream only when WORKER_OWNS_EXECUTION is set; the backend must then be put
in LIVE_EXECUTION_SOURCE=worker so execution isn't run twice.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from ..strategy.evaluator import to_fixed
from .execution import ExecutionService
from .signals import SignalStore, evaluate_live_signal

log = logging.getLogger("worker.execution.live")

LONG = "long"


class LiveExecutor:
    def __init__(
        self,
        signal_store: SignalStore,
        execution: ExecutionService,
        llm: Any,
        pool: Any,
        publish_positions: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.signal_store = signal_store
        self.execution = execution
        self.llm = llm
        self.pool = pool
        self.publish_positions = publish_positions

    async def on_tick(self, ticker_id: str, close: float) -> None:
        # Order matches handleTick: mark prices, then hard exits (checked every tick so a
        # runaway loss is cut intra-candle).
        await self._update_mark_prices(ticker_id, close)
        await self.execution.enforce_hard_exits(ticker_id, close)

    async def on_final_candle(self, ticker_id: str, close: float) -> None:
        signal, strategy_id = await evaluate_live_signal(self.signal_store, self.llm, self.pool, ticker_id, close)
        if signal == "HOLD":
            return
        if signal == "BUY":
            result = await self.execution.execute_trade_signal(ticker_id, LONG, close, strategy_id)
            if result.get("status") == "REJECTED":
                log.warning("Live BUY signal rejected for ticker %s: %s", ticker_id, result.get("reason"))
        elif signal == "SELL":
            open_position = await self.signal_store.get_open_position_for_strategy(ticker_id, strategy_id)
            if open_position:
                await self.execution.close_position(open_position["id"], close)

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
