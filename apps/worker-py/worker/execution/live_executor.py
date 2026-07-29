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
import time
from typing import Any, Awaitable, Callable, Optional

from ..util import to_fixed
from .execution import ExecutionService
from .signals import SignalStore, evaluate_live_signal

log = logging.getLogger("worker.execution.live")

LONG = "long"
SHORT = "short"

# Chart timeframes a live trade is stamped onto (must match the UI's selector), with each bar length
# in ms so the event lands on the right bar for whichever interval is being viewed.
CHART_INTERVALS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
}


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
        if signal in ("BUY", "SHORT"):
            # The signal names the SIDE to open: BUY a long, SHORT a real sell-to-open on the futures
            # venue. A 'both' strategy emits whichever leg its rules selected this bar; the execution
            # engine maps the side to the right exchange order and P&L.
            direction = SHORT if signal == "SHORT" else LONG
            result = await self.execution.execute_trade_signal(ticker_id, direction, close, strategy_id)
            if result.get("status") == "REJECTED":
                log.warning("Live entry signal rejected for ticker %s (%s): %s",
                            ticker_id, direction, result.get("reason"))
            else:
                await self._record_live_signal(ticker_id, strategy_id, direction, opening=True,
                                               price=close, reason="Live entry")
        elif signal == "SELL":
            open_position = await self.signal_store.get_open_position_for_strategy(ticker_id, strategy_id)
            if open_position:
                await self.execution.close_position(open_position["id"], close)
                await self._record_live_signal(ticker_id, strategy_id, open_position.get("side") or LONG,
                                               opening=False, price=close, reason="Live exit")

    async def _record_live_signal(self, ticker_id: str, strategy_id: Optional[str], direction: str,
                                  *, opening: bool, price: float, reason: str) -> None:
        """Persist what the LIVE loop actually did, so the chart is a record of real trades and not
        only a replay reconstruction.

        This was a real gap: markers were written from ONE place — the chart-request path — so a
        position opened live had no marker at all, and the replay's own entry could sit on a
        different bar entirely (a trade opened at 10:32 traced back to a 06:00 replay entry, because
        the loop reconciles to intended exposure rather than waiting for a fresh edge).

        The event is stamped onto every chart timeframe, bucketed to each one's bar, so the trade is
        visible whichever interval is on screen. Where the replay already produced a marker on that
        bar the unique key makes this a no-op."""
        if not strategy_id:
            return
        try:
            side = "sell" if (direction == SHORT) == opening else "buy"
            sig = {"side": side, "direction": direction, "price": price, "reason": reason}
            now_ms = int(time.time() * 1000)
            for interval, step_ms in CHART_INTERVALS.items():
                bar_ms = (now_ms // step_ms) * step_ms
                await self.signal_store.save_signals(
                    strategy_id, ticker_id, interval, direction, [{**sig, "time": bar_ms // 1000}])
        except Exception:  # noqa: BLE001 — recording must never break the trading loop
            log.exception("Failed to record live signal for ticker %s", ticker_id)

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
