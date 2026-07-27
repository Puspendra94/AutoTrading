"""Mode A live-signal tests (Phase 3c-3) — deterministic EMA-crossover rules + dispatcher.

Mode B (AI) is not exercised here (needs an LLM); the dispatcher's Mode A path and the
no-live-strategy short-circuit are covered.
"""
from worker.execution.signals import evaluate_live_signal, evaluate_rules_signal

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
