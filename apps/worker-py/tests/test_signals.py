"""Mode A live-signal tests (Phase 3c-3) — deterministic EMA-crossover rules + dispatcher.

Mode B (AI) is not exercised here (needs an LLM); the dispatcher's Mode A path and the
no-live-strategy short-circuit are covered.
"""
import dataclasses

import worker.execution.signals as signals_mod
from worker.config import config
from worker.execution.signals import apply_hybrid_exit_overlay, evaluate_live_signal, evaluate_rules_signal
from worker.llm.schemas import ExitTightenDecision

PARAMS = {"indicatorConfig": {"emaFastPeriod": 3, "emaSlowPeriod": 5, "stopLossPct": 1.5, "takeProfitPct": 3.5}}
LIVE_A = {"id": "s1", "executionMode": "mode_a_rules", "parametersJson": PARAMS}

# Flat then a sharp jump -> fast EMA crosses above slow at the last bar.
CROSS_UP = [20, 20, 20, 20, 20, 20, 20, 40]
# Steady uptrend -> fast stays above slow (no fresh cross).
UPTREND = [95, 96, 97, 98, 99, 100, 101, 102]


class FakeSignalStore:
    def __init__(self, live=None, closes=None, open_pos=None):
        self._live = live
        self._closes = closes or []
        self._open = open_pos

    async def get_live_strategy(self, t): return self._live
    async def load_recent_closes(self, t, limit): return self._closes[-limit:]
    async def load_recent_candles(self, t, limit):
        # Synthesize OHLC from the close series (O=H=L=C) — these fixtures only exercise
        # close-based EMAs, so flat OHLC is sufficient and keeps the interpreter's decisions
        # identical to the old close-only crossover logic.
        cs = self._closes[-limit:]
        return [{"open": c, "high": c, "low": c, "close": c, "volume": 0, "timestamp": i * 60000}
                for i, c in enumerate(cs)]
    async def load_recent_candles_for_interval(self, t, interval, limit):
        # Fixtures are one bar each; interval aggregation is exercised against the real DB, not here.
        return await self.load_recent_candles(t, limit)
    async def get_open_position_for_strategy(self, t, s): return self._open
    async def get_ticker(self, t): return {"symbol": "BTCUSDT", "interval": "1m"}
    async def retrieve_lessons(self, t, st, limit=5): return []


async def test_flat_bullish_cross_buys():
    store = FakeSignalStore(live=LIVE_A, closes=CROSS_UP, open_pos=None)
    assert await evaluate_rules_signal(store, "t1", LIVE_A) == "BUY"


async def test_flat_no_cross_holds():
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos=None)
    assert await evaluate_rules_signal(store, "t1", LIVE_A) == "HOLD"


async def test_open_position_stop_loss_sells():
    # entry 100, last close 90 -> -10% <= -1.5% stop.
    store = FakeSignalStore(live=LIVE_A, closes=[100, 99, 98, 96, 94, 92, 91, 90], open_pos={"entryPrice": 100})
    assert await evaluate_rules_signal(store, "t1", LIVE_A) == "SELL"


async def test_open_position_within_bounds_holds():
    # entry 99, last 102 -> +3.03% (< 3.5% TP), uptrend so no cross-down.
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"entryPrice": 99})
    assert await evaluate_rules_signal(store, "t1", LIVE_A) == "HOLD"


async def test_insufficient_candles_holds():
    store = FakeSignalStore(live=LIVE_A, closes=[20, 20, 20], open_pos=None)
    assert await evaluate_rules_signal(store, "t1", LIVE_A) == "HOLD"


async def test_dispatcher_no_live_strategy():
    store = FakeSignalStore(live=None)
    assert await evaluate_live_signal(store, None, None, "t1", 100) == ("HOLD", None)


async def test_dispatcher_routes_mode_a():
    store = FakeSignalStore(live=LIVE_A, closes=CROSS_UP, open_pos=None)
    assert await evaluate_live_signal(store, None, None, "t1", 40) == ("BUY", "s1")


# --- Phase 2 hybrid AI exit overlay ---
class FakeLlm:
    def __init__(self, action):
        self._action = action
        self.calls = 0

    async def generate_structured_completion(self, prompt, schema, **kw):
        self.calls += 1
        return {"data": ExitTightenDecision(action=self._action, reasoning="test"),
                "model": "m", "provider": "p", "inputTokens": 1, "outputTokens": 1, "costUsd": 0.0}

    async def log_cost(self, *a, **k):
        pass


async def test_overlay_ai_exit_upgrades_hold_to_sell():
    # Winning open position, rules say HOLD, AI says EXIT -> SELL.
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"id": "p1", "entryPrice": 99, "unrealizedPl": 5})
    llm = FakeLlm("EXIT")
    assert await apply_hybrid_exit_overlay(store, llm, None, "t1", LIVE_A, 102) == "SELL"
    assert llm.calls == 1


async def test_overlay_ai_hold_keeps_hold():
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"id": "p1", "entryPrice": 99, "unrealizedPl": 5})
    llm = FakeLlm("HOLD")
    assert await apply_hybrid_exit_overlay(store, llm, None, "t1", LIVE_A, 102) == "HOLD"


async def test_overlay_skips_losers_without_llm_call():
    # Losing position -> deterministic stop/floor owns it; no LLM spend.
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"id": "p1", "entryPrice": 110, "unrealizedPl": -3})
    llm = FakeLlm("EXIT")
    assert await apply_hybrid_exit_overlay(store, llm, None, "t1", LIVE_A, 102) == "HOLD"
    assert llm.calls == 0


def _config_with(monkeypatch, **overrides):
    # Config is a frozen dataclass; swap the module-level reference for a modified copy.
    monkeypatch.setattr(signals_mod, "config", dataclasses.replace(config, **overrides))


async def test_overlay_never_fires_when_flag_off(monkeypatch):
    # Flag off: dispatcher must not consult the AI even on a winning HOLD.
    _config_with(monkeypatch, hybrid_exit_ai=False)
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"id": "p1", "entryPrice": 99, "unrealizedPl": 5})
    llm = FakeLlm("EXIT")
    sig, _ = await evaluate_live_signal(store, llm, None, "t1", 102)
    assert sig == "HOLD" and llm.calls == 0


async def test_overlay_wired_into_dispatcher_when_flag_on(monkeypatch):
    _config_with(monkeypatch, hybrid_exit_ai=True)
    store = FakeSignalStore(live=LIVE_A, closes=UPTREND, open_pos={"id": "p1", "entryPrice": 99, "unrealizedPl": 5})
    llm = FakeLlm("EXIT")
    sig, sid = await evaluate_live_signal(store, llm, None, "t1", 102)
    assert (sig, sid) == ("SELL", "s1") and llm.calls == 1


# ---------------------------------------------------------------- dual-direction ('both')
RSI = lambda p: {"op": "indicator", "kind": "rsi", "period": p}   # noqa: E731
EMA = lambda p: {"op": "indicator", "kind": "ema", "period": p}   # noqa: E731
CONST = lambda v: {"op": "const", "value": v}                     # noqa: E731
CLOSE = {"op": "price", "field": "close"}

# Symmetric: same rsi(5)/ema(10) on both sides, mirrored thresholds and trend filter.
BOTH_PARAMS = {
    "strategyName": "Symmetric RSI", "direction": "both",
    "entry": {"op": "and", "conditions": [{"op": "lt", "left": RSI(5), "right": CONST(30)},
                                          {"op": "gt", "left": CLOSE, "right": EMA(10)}]},
    "exit": {"op": "gt", "left": RSI(5), "right": CONST(50)},
    "shortEntry": {"op": "and", "conditions": [{"op": "gt", "left": RSI(5), "right": CONST(70)},
                                               {"op": "lt", "left": CLOSE, "right": EMA(10)}]},
    "shortExit": {"op": "lt", "left": RSI(5), "right": CONST(50)},
    "risk": {"stopLossPct": 2, "takeProfitPct": 6},
}
LIVE_BOTH = {"id": "s-both", "executionMode": "mode_a_rules", "parametersJson": BOTH_PARAMS}

# Long decline (price well below its EMA) then a sharp bounce -> RSI overbought while still under
# the EMA: the SHORT leg's setup, and impossible for the long leg (which needs price ABOVE the EMA).
SHORT_SETUP = [200 - 4 * i for i in range(40)] + [42, 52, 64, 78]


async def test_both_strategy_opens_the_short_leg(monkeypatch):
    monkeypatch.setattr(signals_mod, "config", dataclasses.replace(config, futures_execution_enabled=True))
    store = FakeSignalStore(live=LIVE_BOTH, closes=SHORT_SETUP, open_pos=None)
    # Must never be BUY: a long entry requires price above the EMA, which is false here.
    assert await evaluate_rules_signal(store, "t1", LIVE_BOTH) in ("SHORT", "HOLD")


async def test_both_strategy_short_leg_blocked_without_futures(monkeypatch):
    """Futures execution off -> hold the short leg rather than place a wrong-direction long."""
    monkeypatch.setattr(signals_mod, "config", dataclasses.replace(config, futures_execution_enabled=False))
    store = FakeSignalStore(live=LIVE_BOTH, closes=SHORT_SETUP, open_pos=None)
    assert await evaluate_rules_signal(store, "t1", LIVE_BOTH) == "HOLD"


async def test_both_strategy_exit_uses_the_held_side():
    """An open SHORT is judged with short P&L: a price RALLY must stop it out, not profit it."""
    from worker.strategy.dsl.interpreter import exit_reason
    from worker.strategy.dsl.ir import resolve_ir
    ir = resolve_ir(BOTH_PARAMS)
    candles = [{"open": c, "high": c, "low": c, "close": c, "volume": 0, "timestamp": i * 60000}
               for i, c in enumerate([100 + i for i in range(40)])]
    ctx = {"entryPrice": 100.0, "barsHeld": 10, "extremePrice": 100.0}
    assert exit_reason(ir, candles, len(candles) - 1, ctx, direction="short") == "Stop loss"


async def test_both_strategy_costs_about_one_extra_parameter():
    """The whole point of the symmetric shape: shared indicators are counted once, so trading both
    sides costs ~1 knob rather than doubling (which would blow the overfitting budget)."""
    from worker.strategy.dsl.ir import count_ir_parameters, resolve_ir
    long_only = {k: v for k, v in BOTH_PARAMS.items() if k not in ("shortEntry", "shortExit")}
    long_only["direction"] = "long"
    assert count_ir_parameters(resolve_ir(BOTH_PARAMS)) - count_ir_parameters(resolve_ir(long_only)) <= 2


async def test_both_strategy_requires_its_short_side():
    """direction='both' without shortEntry/shortExit is rejected — otherwise the short leg would
    silently reuse the long rules and trade backwards."""
    from worker.strategy.dsl.ir import validate_ir
    broken = {k: v for k, v in BOTH_PARAMS.items() if k not in ("shortEntry", "shortExit")}
    errs = validate_ir(broken)
    assert any("shortEntry" in e for e in errs) and any("shortExit" in e for e in errs)


# ---------------------------------------------------------------- live decisions become markers
class _RecordingSignalStore:
    """Minimal SignalStore that captures save_signals calls."""
    def __init__(self, live, position=None):
        self._live, self._position = live, position
        self.saved: list[dict] = []

    async def get_live_strategy(self, t): return self._live
    async def get_open_position_for_strategy(self, t, s): return self._position
    async def save_signals(self, strategy_id, ticker_id, interval, direction, signals):
        for s in signals:
            self.saved.append({"interval": interval, "direction": direction, **s})


class _Exec:
    def __init__(self, status="EXECUTED"):
        self.status, self.closed = status, []
        self.store = None
    async def execute_trade_signal(self, *a, **k): return {"status": self.status}
    async def close_position(self, pid, price): self.closed.append(pid)


async def test_live_entry_is_recorded_as_a_marker():
    """Markers used to be written from ONE place — the chart-request path — so a live trade left no
    marker at all and the user could not see what opened their position."""
    from worker.execution.live_executor import CHART_INTERVALS, LiveExecutor
    store = _RecordingSignalStore(live={"id": "s1"})
    ex = LiveExecutor(store, _Exec(), None, None)
    await ex._record_live_signal("t1", "s1", "short", opening=True, price=64620.0, reason="Live entry")
    assert len(store.saved) == len(CHART_INTERVALS)          # visible on every chart timeframe
    assert {s["interval"] for s in store.saved} == set(CHART_INTERVALS)
    one = store.saved[0]
    assert one["direction"] == "short" and one["side"] == "sell"   # a short OPENS by selling
    assert one["price"] == 64620.0 and one["reason"] == "Live entry"


async def test_live_exit_records_the_covering_side():
    from worker.execution.live_executor import LiveExecutor
    store = _RecordingSignalStore(live={"id": "s1"})
    ex = LiveExecutor(store, _Exec(), None, None)
    await ex._record_live_signal("t1", "s1", "short", opening=False, price=64000.0, reason="Live exit")
    assert store.saved[0]["side"] == "buy"      # a short CLOSES by buying back


async def test_rejected_entry_records_nothing():
    """A rejected order (e.g. size rounds to zero) must not leave a phantom marker."""
    from worker.execution.live_executor import LiveExecutor
    store = _RecordingSignalStore(live={"id": "s1"})
    ex = LiveExecutor(store, _Exec(status="REJECTED"), None, None)
    import worker.execution.live_executor as lm
    async def fake_eval(*a, **k): return ("SHORT", "s1")
    orig, lm.evaluate_live_signal = lm.evaluate_live_signal, fake_eval
    try:
        await ex.on_final_candle("t1", 64620.0)
    finally:
        lm.evaluate_live_signal = orig
    assert store.saved == []
