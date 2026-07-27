"""LiveExecutor dispatch tests (Phase 3c-3) — the tick → evaluate → execute wiring.

Uses fakes for the signal store and the execution engine, so no risk gate, DB, or Binance
is touched; verifies BUY→execute, SELL→close, HOLD→nothing, and on_tick's mark-price + hard
exits + positions:update publish.
"""
from worker.execution.live_executor import LiveExecutor

PARAMS = {"indicatorConfig": {"emaFastPeriod": 3, "emaSlowPeriod": 5, "stopLossPct": 1.5, "takeProfitPct": 3.5}}
LIVE_A = {"id": "s1", "executionMode": "mode_a_rules", "parametersJson": PARAMS}
CROSS_UP = [20, 20, 20, 20, 20, 20, 20, 40]
STOP_LOSS = [100, 99, 98, 96, 94, 92, 91, 90]


class FakeSignalStore:
    def __init__(self, live=None, closes=None, open_pos=None):
        self._live, self._closes, self._open = live, closes or [], open_pos

    async def get_live_strategy(self, t): return self._live
    async def load_recent_closes(self, t, limit): return self._closes[-limit:]
    async def load_recent_candles(self, t, limit):
        cs = self._closes[-limit:]
        return [{"open": c, "high": c, "low": c, "close": c, "volume": 0, "timestamp": i * 60000}
                for i, c in enumerate(cs)]
    async def load_recent_candles_for_interval(self, t, interval, limit):
        return await self.load_recent_candles(t, limit)
    async def get_open_position_for_strategy(self, t, s): return self._open
    async def get_ticker(self, t): return {"symbol": "BTCUSDT", "interval": "1m"}
    async def retrieve_lessons(self, t, st, limit=5): return []


class FakeExecStore:
    def __init__(self, positions): self.positions, self.marks = positions, []
    async def find_open_positions_by_ticker(self, t): return self.positions
    async def update_position_mark(self, pid, cp, upl): self.marks.append((pid, cp, upl))


class FakeExec:
    def __init__(self, positions=None):
        self.store = FakeExecStore(positions or [])
        self.hard_exits, self.trades, self.closes = [], [], []

    async def enforce_hard_exits(self, t, price): self.hard_exits.append((t, price))
    async def execute_trade_signal(self, t, side, price, sid):
        self.trades.append((t, side, price, sid)); return {"status": "EXECUTED"}
    async def close_position(self, pid, price):
        self.closes.append((pid, price)); return {"positionId": pid, "realizedPl": 0}


def _executor(signal_store, exec_engine, published):
    async def publish(): published.append(True)
    return LiveExecutor(signal_store, exec_engine, llm=None, pool=None, publish_positions=publish)


async def test_on_final_buy_executes():
    ex = FakeExec()
    await _executor(FakeSignalStore(live=LIVE_A, closes=CROSS_UP, open_pos=None), ex, []).on_final_candle("t1", 40)
    assert ex.trades == [("t1", "long", 40, "s1")]
    assert ex.closes == []


async def test_on_final_sell_closes_open_position():
    ex = FakeExec()
    store = FakeSignalStore(live=LIVE_A, closes=STOP_LOSS, open_pos={"id": "pos1", "entryPrice": 100})
    await _executor(store, ex, []).on_final_candle("t1", 90)
    assert ex.closes == [("pos1", 90)]
    assert ex.trades == []


async def test_on_final_hold_does_nothing():
    ex = FakeExec()
    await _executor(FakeSignalStore(live=None), ex, []).on_final_candle("t1", 100)
    assert ex.trades == [] and ex.closes == []


async def test_on_tick_marks_prices_and_enforces_exits():
    ex = FakeExec(positions=[{"id": "p1", "side": "long", "entryPrice": 100, "quantity": 2}])
    published = []
    await _executor(FakeSignalStore(), ex, published).on_tick("t1", 110)
    assert ex.hard_exits == [("t1", 110)]
    assert ex.store.marks == [("p1", 110, 20.0)]  # unrealized (110-100)*2
    assert published == [True]


async def test_on_tick_no_positions_no_publish():
    ex = FakeExec(positions=[])
    published = []
    await _executor(FakeSignalStore(), ex, published).on_tick("t1", 110)
    assert ex.hard_exits == [("t1", 110)]
    assert ex.store.marks == [] and published == []
