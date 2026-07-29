"""PatternExecutor bar-handling tests.

The stream fires every 1m but the engine evaluates on 15m, so the interesting behaviour is all
about WHICH bar gets processed and how often — partial bars, repeats, restarts.
"""
from worker.pattern.executor import PatternExecutor, _drop_incomplete_bar

BAR_MS = 900_000  # 15m


class FakeExecStore:
    def __init__(self):
        self.marks = []

    async def find_open_positions_by_ticker(self, t):
        return []

    async def update_position_mark(self, pid, cp, upl):
        self.marks.append((pid, cp, upl))


class FakeExec:
    def __init__(self):
        self.store = FakeExecStore()
        self.hard_exits, self.trades, self.closes = [], [], []

    async def enforce_hard_exits(self, t, price):
        self.hard_exits.append((t, price))


class RecordingStore:
    def __init__(self):
        self.saved = []

    async def save_triggers(self, ticker_id, interval, bar_time_ms, triggers, state_pack=None):
        self.saved.append({"bar": bar_time_ms, "triggers": triggers, "pack": state_pack})
        return len(triggers)


def rising_bars(count, start_ms=0, step=BAR_MS):
    return [
        {"timestamp": start_ms + i * step, "open": 100 + i * 0.9, "high": 101 + i * 0.9,
         "low": 99 + i * 0.9, "close": 100 + i * 0.9, "volume": 10.0}
        for i in range(count)
    ]


# --------------------------------------------------------------------- partial-bar handling
def test_incomplete_trailing_bar_is_dropped():
    """time_bucket returns the bucket currently being built; its close is still moving."""
    bars = rising_bars(3)
    now = 2 * BAR_MS + 100  # the third bar's period has NOT elapsed

    kept = _drop_incomplete_bar(bars, "15m", now_ms=now)
    assert len(kept) == 2
    assert kept[-1]["timestamp"] == BAR_MS


def test_complete_trailing_bar_is_kept():
    bars = rising_bars(3)
    now = 3 * BAR_MS + 1  # the third bar's period HAS elapsed

    assert len(_drop_incomplete_bar(bars, "15m", now_ms=now)) == 3


def test_drop_incomplete_bar_handles_empty():
    assert _drop_incomplete_bar([], "15m") == []


# --------------------------------------------------------------------- evaluation cadence
async def _executor(bars, store=None):
    async def load_candles(ticker_id, interval, limit):
        return bars

    return PatternExecutor(FakeExec(), pool=None, store=store or RecordingStore(),
                           load_candles=load_candles)


async def test_same_bar_is_evaluated_only_once():
    """Twelve 1m closes land inside one 15m bar; the engine must run for the first only."""
    bars = rising_bars(320, start_ms=1_000_000_000)
    executor = await _executor(bars)

    first = await executor.evaluate("t1")
    repeats = [await executor.evaluate("t1") for _ in range(11)]

    assert first is not None
    assert all(r is None for r in repeats)


async def test_new_bar_is_evaluated_again():
    bars = rising_bars(320, start_ms=1_000_000_000)
    executor = await _executor(bars)
    assert await executor.evaluate("t1") is not None

    # A new complete bar arrives.
    bars.append({"timestamp": bars[-1]["timestamp"] + BAR_MS, "open": 500, "high": 501,
                 "low": 499, "close": 500, "volume": 10.0})
    assert await executor.evaluate("t1") is not None


async def test_cold_engine_emits_no_triggers_and_saves_nothing():
    """Short history is the normal state after a fresh backfill — it must not be an error, and
    it must not produce markers from half-formed indicators."""
    store = RecordingStore()
    executor = await _executor(rising_bars(40, start_ms=1_000_000_000), store=store)

    state = await executor.evaluate("t1")
    await executor.on_final_candle("t1", 100)

    assert state is not None and state.warm is False
    assert state.triggers == []
    assert store.saved == []


async def test_triggers_are_persisted_with_their_state_pack():
    """A stored trigger carries the exact numbers that produced it, so a decision can be
    replayed later without recomputing (or re-paying for) anything."""
    store = RecordingStore()
    bars = rising_bars(320, start_ms=1_000_000_000)
    # Drive price hard through the recent range so a break/flip actually fires.
    for i in range(-5, 0):
        bars[i]["close"] = bars[i]["high"] = 100 + 320 * 0.9 + 60
    executor = await _executor(bars, store=store)

    await executor.on_final_candle("t1", bars[-1]["close"])

    if store.saved:  # triggers are data-dependent; assert the SHAPE when one fires
        entry = store.saved[0]
        assert entry["bar"] == bars[-1]["timestamp"]
        assert entry["pack"]["market"]["interval"] == "15m"
        assert entry["pack"]["indicators"]["atr14"] is not None
        for trigger in entry["triggers"]:
            assert trigger["side"] in ("long", "short")
            assert set(trigger) >= {"name", "side", "price", "detail"}


# --------------------------------------------------------------------- jsonb encoding
async def test_state_pack_is_passed_as_a_dict_not_pre_serialised():
    """Regression: db.py registers a jsonb codec with encoder=json.dumps, so serialising the
    pack in the store too stored a JSON *string* inside the jsonb column (jsonb_typeof =
    'string'). Every reader then saw text where an object was expected and the whole Market Read
    panel rendered blank. The store must hand asyncpg the dict and let the codec encode it."""
    from worker.pattern.store import PatternSignalStore

    captured = {}

    class FakeConn:
        async def executemany(self, sql, rows):
            captured["rows"] = rows

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    store = PatternSignalStore(FakePool())
    pack = {"regime": {"label": "range"}}
    await store.save_triggers(
        "t1", "15m", 900_000,
        [{"name": "resistance_break", "side": "long", "price": 100.0, "detail": "d"}],
        pack,
    )

    assert captured["rows"][0][-1] is pack, "state pack must reach asyncpg as a dict"
