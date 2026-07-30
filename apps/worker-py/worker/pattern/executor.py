"""Live loop for the pattern brain — the TRADING_BRAIN='pattern' counterpart of
execution/live_executor.py::LiveExecutor.

Duck-typed against LiveExecutor (`on_tick` / `on_final_candle`) so live_stream.py drives
whichever brain the factory built without knowing which one it got.

PHASE 2: the feature engine runs, its triggers are persisted as chart markers, and every closed
bar is put through the decision loop (gate -> LLM -> validate -> size -> risk gate -> order).
Exits are still only the deterministic stop/target recorded at entry; the full exit ladder is
Phase 3.

The stream fires on every 1m close, but the engine evaluates on `config.pattern_interval`
(15m). Rather than counting ticks, each 1m close asks the DB for the rolled-up bars and
processes only when a NEW COMPLETE bar has appeared. That is naturally correct across restarts,
reconnects and replayed closes, none of which a counter would survive.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..candles import get_candles
from ..config import config
from ..exchange_info import get_symbol_filters
from ..execution.risk_gate import PAPER_NOTIONAL
from ..util import to_fixed
from .decision.engine import build_account_snapshot
from .exits import better_extreme, evaluate_exit
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
        decisions: Optional[Any] = None,
    ) -> None:
        self.execution = execution
        self.pool = pool
        self.publish_positions = publish_positions
        self.store = store if store is not None else (PatternSignalStore(pool) if pool else None)
        # Injectable so the engine can be driven from a test or a historical replay without a DB.
        self.load_candles = load_candles or get_candles
        # None disables the decision loop entirely — the engine then only observes and records
        # features, which is exactly the Phase 1 behaviour and a useful dry-run mode.
        self.decisions = decisions
        self.interval = config.pattern_interval
        self.candle_limit = config.pattern_candle_limit
        self._last_bar_ms: dict[str, int] = {}
        self._warned_cold = False
        # Latest ATR per ticker, refreshed on each candle close. The tick loop needs it to size
        # the trailing stop but must not recompute indicators 60 times a bar to get it.
        self._atr_by_ticker: dict[str, float] = {}

    async def on_tick(self, ticker_id: str, close: float) -> None:
        # Mark first, then run the ladder. Deliberately NOT calling execution.enforce_hard_exits:
        # those are the provider-level percentage caps the strategy brain relies on, and running
        # both would mean two exit systems acting on one position.
        await self._update_mark_prices(ticker_id, close)
        await self._enforce_exit_ladder(ticker_id, close)

    async def _enforce_exit_ladder(self, ticker_id: str, price: float) -> None:
        """Run the deterministic ladder over every open pattern position.

        On EVERY tick, not just candle closes: a stop that is only checked every 15 minutes is not
        a stop. This is what makes "let the target run" safe — protection ratchets continuously
        while the LLM is consulted at most once a bar.
        """
        if self.store is None:
            return
        try:
            positions = await self.store.open_pattern_positions(ticker_id)
        except Exception:  # noqa: BLE001 — never let the ladder break ingestion
            log.exception("Could not load pattern positions for %s", ticker_id)
            return
        if not positions:
            return

        atr = self._atr_by_ticker.get(ticker_id, 0.0)
        for position in positions:
            try:
                await self._apply_exit_action(position, price, atr)
            except Exception:  # noqa: BLE001 — one bad position must not stop the others
                log.exception("Exit ladder failed for position %s", position["id"])

    async def _apply_exit_action(self, position: dict, price: float, atr: float) -> None:
        side = position.get("side") or LONG
        bars = _bars_held(position.get("openedAt"), self.interval)
        action = evaluate_exit(position, price, atr, bars_held=bars)

        if action.is_exit:
            log.warning("EXIT position %s @ %.2f — %s", position["id"], price, action.reason)
            await self.store.record_exit_reason(position["id"], action.reason)
            await self.execution.close_position(position["id"], price)
            if self.publish_positions:
                await self.publish_positions()
            return

        if action.is_ratchet and action.new_stop is not None:
            extreme = better_extreme(side, _num_or_none(position.get("extremePrice")), price)
            await self.store.ratchet_stop(
                position["id"], new_stop=action.new_stop,
                lifecycle=action.new_lifecycle or position.get("lifecycle") or "open",
                extreme_price=extreme,
            )
            log.info("Position %s: %s", position["id"], action.reason)
            return

        # No action, but the high-water mark still moves — it is what the trail measures from.
        await self.store.update_extreme(position["id"], side, price)

    async def on_final_candle(self, ticker_id: str, close: float) -> None:
        """Evaluate features when a new COMPLETE bar of the decision interval has closed, then
        put that bar through the decision loop."""
        state = await self.evaluate(ticker_id)
        if state is None:
            return

        # Heartbeat on every evaluated bar. Without it a stalled decision loop is indistinguishable
        # from a quiet market in the logs — which is exactly how a ten-hour gap went unnoticed.
        log.info(
            "Evaluated %s bar %d: regime=%s bias=%s triggers=%d warm=%s",
            self.interval, state.bar_time_ms, state.regime["label"], state.bias,
            len(state.triggers), state.warm,
        )

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

        if self.decisions is not None:
            await self._run_decision(state)

    async def _run_decision(self, state) -> None:
        """One bar through the decision loop. Every bar produces a recorded decision, including
        the free ones where the gate declined — a gate that is too tight is invisible otherwise."""
        try:
            account, open_position = await self._account_and_position(state.ticker_id)
        except Exception as err:  # noqa: BLE001 — never let account lookup break ingestion
            # Record the failure as a decision instead of returning silently. This module claims
            # "every evaluated bar produces exactly one decision", and a bare `return` broke that
            # promise in the worst possible way: a stalled decision loop looked EXACTLY like a
            # quiet market — no rows, no trades, nothing to notice. An error row makes a stall
            # visible in the same feed you already read.
            log.exception("Could not build the account snapshot for %s", state.ticker_id)
            await self._persist(self._error_decision(state, f"Account snapshot failed: {err}"))
            return

        failures = []
        if self.store is not None and not open_position:
            try:
                failures = await self.store.recent_failures(state.ticker_id)
            except Exception:  # noqa: BLE001 — prompt context is optional, not load-bearing
                log.warning("Could not load recent failures", exc_info=True)

        decision = await self.decisions.decide(
            state, open_position=open_position, account=account, failures=failures,
        )
        await self._persist(decision)

    async def _persist(self, decision: dict) -> None:
        if self.store is None:
            return
        try:
            await self.store.save_decision(decision)
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist the decision for %s", decision.get("tickerId"))

    def _error_decision(self, state, reason: str) -> dict:
        """A minimal decision row for a failure that happened before the engine could run."""
        return {
            "tickerId": state.ticker_id, "interval": state.interval,
            "barTimeMs": state.bar_time_ms, "outcome": "error", "reason": reason,
            "gate": None, "statePack": state.to_state_pack(), "llmDecision": None,
            "validation": None, "sizing": None, "positionId": None,
            "stopPrice": None, "takeProfit": None, "model": None, "costUsd": 0.0,
            "inputTokens": 0, "outputTokens": 0,
        }

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

        symbol = await self._symbol_for(ticker_id)
        state = build_feature_state(ticker_id, symbol, self.interval, candles)
        # Marked processed only AFTER the state is built. Setting it first meant a transient
        # failure in build_feature_state permanently consumed that bar — it would never be retried
        # and would leave no trace.
        self._last_bar_ms[ticker_id] = bar_ms

        # Cache ATR for the tick-rate exit ladder (see _enforce_exit_ladder).
        atr = state.summary.get("atr14")
        if atr and not math.isnan(atr):
            self._atr_by_ticker[ticker_id] = float(atr)

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

    async def _account_and_position(self, ticker_id: str) -> tuple[dict, Optional[dict]]:
        """Gather the money facts a decision needs, plus any open position on this ticker.

        The daily risk budget is a percentage of the balance the DAY started with, so it is read
        from daily_loss_tracking.capital_under_management_base — the snapshot taken when today's
        row was created — rather than from the balance right now, which drifts with every open
        position's mark price. The row is created here if today's is missing, so the budget is
        anchored by the first evaluation after 00:00 UTC (within one bar) rather than waiting for
        the first order attempt.
        """
        risk_store = self.execution.risk_store
        # The EXECUTION store's get_ticker, not the risk gate's: the latter returns only
        # {id, providerId}, so symbol came back empty and marketType None. That silently fetched
        # SPOT filters for a futures ticker — and since the offline fallback happens to match
        # BTCUSDT futures, the wrong venue's rules (minNotional 5 vs 50, a far finer stepSize)
        # would have gone unnoticed.
        ticker = await self.execution.store.get_ticker(ticker_id)
        if not ticker:
            raise ValueError(f"Unknown ticker {ticker_id}")
        provider_id = ticker["providerId"]
        provider = await risk_store.get_provider(provider_id)
        is_paper = (provider or {}).get("tradingMode", "paper") != "live"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tracker = await risk_store.get_today_tracker(provider_id, today)
        if not tracker:
            if is_paper:
                base = PAPER_NOTIONAL
            else:
                balance = await risk_store.get_latest_balance(provider_id)
                base = float(balance["tradableBalance"]) if balance else PAPER_NOTIONAL
            await risk_store.create_today_tracker(provider_id, today, base)
            day_start_balance = base
        else:
            day_start_balance = float(tracker.get("cumBase") or PAPER_NOTIONAL)

        realized_today, open_notional = await self._todays_pnl(ticker_id)
        free_balance = max(day_start_balance + realized_today - open_notional, 0.0)

        filters = await get_symbol_filters(
            ticker.get("symbol") or "", futures=(ticker.get("marketType") or "").lower() == "futures",
        )
        account = build_account_snapshot(
            day_start_balance=day_start_balance,
            # A positive number of dollars LOST. A profitable day contributes nothing to the
            # loss budget — profits do not buy extra risk.
            realized_loss_today=max(-realized_today, 0.0),
            free_balance=free_balance,
            filters=filters,
        )

        # open_pattern_positions filters on strategy_id IS NULL (this brain never sets one) AND
        # returns the ladder state, which the gate needs: a RUNNER earns a look every bar.
        mine = await self.store.open_pattern_positions(ticker_id) if self.store else []
        return account, (mine[0] if mine else None)

    async def _todays_pnl(self, ticker_id: str) -> tuple[float, float]:
        """(realized P&L since 00:00 UTC, notional currently tied up in open positions)."""
        if self.pool is None:
            return 0.0, 0.0
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COALESCE((SELECT sum(realized_pl) FROM positions
                            WHERE ticker_id = $1 AND strategy_id IS NULL AND status = 'closed'
                              AND closed_at >= date_trunc('day', now() AT TIME ZONE 'UTC')), 0) AS realized,
                  COALESCE((SELECT sum(entry_price * quantity) FROM positions
                            WHERE ticker_id = $1 AND strategy_id IS NULL AND status = 'open'), 0) AS open_notional
                """,
                ticker_id,
            )
        return float(row["realized"] or 0.0), float(row["open_notional"] or 0.0)

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


def _num_or_none(value):
    return None if value is None else float(value)


def _bars_held(opened_at, interval: str) -> int:
    """Bars elapsed since the position opened, in units of the decision interval."""
    if not opened_at:
        return 0
    elapsed_ms = (datetime.now(timezone.utc) - opened_at).total_seconds() * 1000
    return max(int(elapsed_ms / interval_ms(interval)), 0)


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
