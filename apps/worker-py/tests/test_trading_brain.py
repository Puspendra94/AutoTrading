"""TRADING_BRAIN isolation tests (Phase 0 of the pattern-brain pivot).

The strategy engine is switched off by configuration, not deleted, so these tests pin the
guarantees that make that safe:

  * the brain is chosen once, at construction — only ONE executor is ever built;
  * a typo'd brain name raises instead of silently falling back to 'strategy';
  * the pattern brain marks positions but does NOT run the strategy brain's hard exits,
    so two exit systems can never act on one position;
  * the pattern brain opens nothing and closes nothing while it is inert.

Fakes throughout — no DB, no risk gate, no Binance.
"""
import dataclasses

import pytest

from worker.execution import factory
from worker.pattern.executor import PatternExecutor


def _with_brain(monkeypatch, brain: str) -> None:
    """Config is a frozen dataclass, so swap in a copy rather than assigning to the field."""
    monkeypatch.setattr(factory, "config", dataclasses.replace(factory.config, trading_brain=brain))


class FakeExecStore:
    def __init__(self, positions):
        self.positions, self.marks = positions, []

    async def find_open_positions_by_ticker(self, t):
        return self.positions

    async def update_position_mark(self, pid, cp, upl):
        self.marks.append((pid, cp, upl))


class FakeExec:
    """Records anything that would move money. The pattern brain must leave these empty."""

    def __init__(self, positions=None):
        self.store = FakeExecStore(positions or [])
        self.hard_exits, self.trades, self.closes = [], [], []

    async def enforce_hard_exits(self, t, price):
        self.hard_exits.append((t, price))

    async def execute_trade_signal(self, t, side, price, sid):
        self.trades.append((t, side, price, sid))
        return {"status": "EXECUTED"}

    async def close_position(self, pid, price):
        self.closes.append((pid, price))
        return {"positionId": pid, "realizedPl": 0}


LONG_POS = {"id": "p1", "side": "long", "entryPrice": "100", "quantity": "2"}


async def test_pattern_executor_marks_positions_without_touching_exits():
    """Mark-to-market is brain-agnostic bookkeeping and must still run; the strategy brain's
    provider-level hard stop/take-profit must NOT, or it would close pattern-brain positions."""
    ex = FakeExec([LONG_POS])
    published = []

    async def publish():
        published.append(True)

    await PatternExecutor(ex, pool=None, publish_positions=publish).on_tick("t1", 110)

    assert ex.store.marks == [("p1", 110, 20.0)]  # (110 - 100) * 2
    assert published == [True]
    assert ex.hard_exits == []


async def test_pattern_executor_places_no_orders_on_candle_close():
    """Phase 1 evaluates features and writes markers; it must still move no money."""
    ex = FakeExec([LONG_POS])
    saved = []

    class FakeStore:
        async def save_triggers(self, *args, **kwargs):
            saved.append(args)
            return 0

    async def load_candles(ticker_id, interval, limit):
        # Enough warm history, trending up, so the engine really does run end to end.
        return [
            {"timestamp": i * 900_000, "open": 100 + i, "high": 101 + i,
             "low": 99 + i, "close": 100 + i, "volume": 10.0}
            for i in range(320)
        ]

    executor = PatternExecutor(ex, pool=None, store=FakeStore(), load_candles=load_candles)
    for _ in range(3):
        await executor.on_final_candle("t1", 110)

    assert ex.trades == []
    assert ex.closes == []
    assert ex.hard_exits == []


async def test_unknown_brain_raises_rather_than_defaulting(monkeypatch):
    _with_brain(monkeypatch, "patern")  # typo
    with pytest.raises(ValueError, match="Unknown TRADING_BRAIN"):
        factory.build_live_executor(pool=None)


async def test_factory_builds_only_the_selected_brain(monkeypatch):
    """The two brains are mutually exclusive by construction — the object returned for
    'pattern' has no strategy signal store at all, so it cannot evaluate a live strategy."""
    _with_brain(monkeypatch, "pattern")
    executor = factory.build_live_executor(pool=None)

    assert isinstance(executor, PatternExecutor)
    assert not hasattr(executor, "signal_store")
    # Duck-typed against LiveExecutor so live_stream.py drives either without knowing which.
    assert callable(executor.on_tick) and callable(executor.on_final_candle)
