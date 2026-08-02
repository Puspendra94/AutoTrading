"""Gate and decision-engine tests — when we pay for an LLM call, and what we refuse to do."""
from dataclasses import dataclass

import pytest

from worker.exchange_info import SymbolFilters
from worker.pattern.decision import engine as engine_mod
from worker.pattern.decision.engine import DecisionEngine, build_account_snapshot
from worker.pattern.decision.gate import COOLDOWN_BARS, evaluate_gate
from worker.pattern.decision.schemas import EntryDecision

BTC = SymbolFilters("BTCUSDT", step_size=0.001, min_qty=0.001, min_notional=50.0, tick_size=0.1)
PRICE = 63800.0


@dataclass
class FakeState:
    """Stand-in for FeatureState — only what the gate and engine actually read."""
    warm: bool = True
    regime: dict = None
    triggers: list = None
    bias: str = "long"
    close: float = PRICE
    ticker_id: str = "t1"
    interval: str = "15m"
    bar_time_ms: int = 1_700_000_000_000

    def __post_init__(self):
        if self.regime is None:
            self.regime = {"label": "uptrend", "adx": 30.0, "emaStack": "20>50>200"}
        if self.triggers is None:
            self.triggers = []

    def to_state_pack(self):
        return {
            "market": {"symbol": "BTCUSDT", "interval": self.interval, "close": self.close,
                       "barTime": self.bar_time_ms, "tickerId": self.ticker_id},
            "regime": self.regime,
            "levels": {"support": [], "resistance": []},
            "structure": {},
            "patterns": {"chart": [], "candle": []},
            "indicators": {"atrPct": 0.356, "rsi14": 55.0, "atr14": 227.0},
            "triggers": self.triggers,
        }


def trigger(name="resistance_break", side="long"):
    return {"name": name, "side": side, "price": PRICE, "detail": "d"}


LONG_POSITION = {"id": "p1", "side": "long", "entryPrice": PRICE, "quantity": 0.01}


# --------------------------------------------------------------------------- gate: free skips
def test_cold_engine_never_pays_for_a_call():
    r = evaluate_gate(FakeState(warm=False, triggers=[trigger()]), None)
    assert not r.should_call and "warming up" in r.reason


def test_no_trigger_is_a_free_skip():
    r = evaluate_gate(FakeState(), None)
    assert not r.should_call and "No trigger" in r.reason


def test_range_regime_still_reaches_the_model():
    """A range is not a reason to stay out — its edges are tradeable, and judging that is the
    model's job. The regime is context in the prompt, not a veto in the gate."""
    state = FakeState(regime={"label": "range", "adx": 15.0, "emaStack": "mixed"},
                      triggers=[trigger()], bias="none")
    r = evaluate_gate(state, None)
    assert r.should_call and r.kind == "entry"


def test_trigger_against_a_weak_trend_still_reaches_the_model():
    state = FakeState(triggers=[trigger(side="short")], bias="long")  # regime is plain 'uptrend'
    assert evaluate_gate(state, None).should_call


def test_fading_a_strong_trend_is_refused_before_paying():
    """The one deterministic refusal kept: do not stand in front of a strong trend."""
    state = FakeState(regime={"label": "strong_uptrend", "adx": 45.0, "emaStack": "20>50>200"},
                      triggers=[trigger(side="short")], bias="long")
    r = evaluate_gate(state, None)
    assert not r.should_call and "strong trend" in r.reason


def test_cooldown_blocks_immediate_re_entry():
    state = FakeState(triggers=[trigger()])
    assert not evaluate_gate(state, None, bars_since_last_close=0).should_call
    assert evaluate_gate(state, None, bars_since_last_close=COOLDOWN_BARS).should_call


def test_exhausted_budget_stops_entries():
    state = FakeState(triggers=[trigger()])
    r = evaluate_gate(state, None, daily_budget_exhausted=True)
    assert not r.should_call and "budget exhausted" in r.reason


# --------------------------------------------------------------------------- gate: paid calls
def test_aligned_trigger_while_flat_is_worth_a_call():
    r = evaluate_gate(FakeState(triggers=[trigger()]), None)
    assert r.should_call and r.kind == "entry"


def test_aligned_position_costs_nothing():
    """A bullish break while already long tells us nothing we don't know."""
    state = FakeState(triggers=[trigger()])
    r = evaluate_gate(state, LONG_POSITION)
    assert not r.should_call and "still aligned" in r.reason


def test_opposing_trigger_on_an_open_position_is_worth_a_call():
    state = FakeState(triggers=[trigger(name="regime_flip", side="short")])
    r = evaluate_gate(state, LONG_POSITION)
    assert r.should_call and r.kind == "exit"


def test_regime_turning_against_a_position_is_worth_a_call():
    state = FakeState(regime={"label": "downtrend", "adx": 30.0, "emaStack": "20<50<200"}, bias="short")
    r = evaluate_gate(state, LONG_POSITION)
    assert r.should_call and r.kind == "exit"


# --------------------------------------------------------------------------- engine safety
class FakeLlm:
    def __init__(self, data=None, model="deepseek-v4-flash"):
        self.data, self.model, self.calls = data, model, 0

    async def generate_structured_completion(self, prompt, schema, max_tokens=1024):
        self.calls += 1
        return {"data": self.data, "model": self.model, "costUsd": 0.001,
                "inputTokens": 10, "outputTokens": 10}

    async def log_cost(self, *a, **kw):
        return None


class FakeExecution:
    def __init__(self):
        self.trades, self.closes = [], []

    async def execute_trade_signal(self, ticker_id, side, price, strategy_id,
                                   requested_quantity=None, requested_leverage=1):
        self.trades.append({"side": side, "price": price, "qty": requested_quantity,
                            "leverage": requested_leverage})
        return {"status": "EXECUTED", "positionId": "new-pos"}

    async def close_position(self, position_id, price):
        self.closes.append((position_id, price))
        return {"positionId": position_id, "realizedPl": 0}


def _account(**kw):
    base = dict(day_start_balance=10000.0, realized_loss_today=0.0, free_balance=10000.0, filters=BTC)
    base.update(kw)
    return build_account_snapshot(**base)


def _engine(llm, execution=None):
    return DecisionEngine(llm, execution or FakeExecution(), store=None, pool=None)


async def test_gate_skip_records_a_decision_without_calling_the_llm():
    """Free skips are still logged — a gate that is too tight is invisible otherwise."""
    llm = FakeLlm()
    result = await _engine(llm).decide(FakeState(), open_position=None, account=_account())

    assert llm.calls == 0
    assert result["outcome"] == engine_mod.SKIPPED_NO_TRIGGER
    assert result["reason"]


async def test_synthetic_fallback_never_becomes_a_trade():
    """LlmService returns a canned '-fallback' response when every model fails. Trading on it
    would be trading on a fabrication."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"),
                  model="deepseek-v4-flash-fallback")
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account())

    assert result["outcome"] == engine_mod.ERROR
    assert "synthetic fallback" in result["reason"]
    assert execution.trades == []


async def test_model_skip_is_recorded_and_places_nothing():
    llm = FakeLlm(data=EntryDecision(action="SKIP", reasoning="no clean setup"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account())

    assert result["outcome"] == engine_mod.SKIPPED_BY_MODEL
    assert execution.trades == []


async def test_fading_a_strong_trend_is_rejected_not_repaired():
    """The validator may only reject. The model's original numbers stay in the log unmodified —
    repairing them would put a trade nobody proposed into the audit trail."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="short", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 1.01,
                                     take_profit=PRICE * 0.98, reasoning="fade it"))
    execution = FakeExecution()
    state = FakeState(regime={"label": "strong_uptrend", "adx": 45.0, "emaStack": "20>50>200"},
                      triggers=[trigger()])
    result = await _engine(llm, execution).decide(state, open_position=None, account=_account())

    assert result["outcome"] == engine_mod.REJECTED
    assert "strong trend" in result["reason"]
    assert execution.trades == []
    assert result["llmDecision"]["side"] == "short"  # stored as proposed, unrepaired


async def test_short_in_a_weak_uptrend_is_allowed_through():
    """Regression against the over-constraint: a weak trend must not veto the model's judgement."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="short", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 1.01,
                                     take_profit=PRICE * 0.97, reasoning="range top"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account())

    assert result["outcome"] == engine_mod.EXECUTED
    assert execution.trades[0]["side"] == "short"


async def test_valid_entry_executes_with_a_deterministically_sized_quantity():
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="break held"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account())

    assert result["outcome"] == engine_mod.EXECUTED
    assert len(execution.trades) == 1
    trade = execution.trades[0]
    assert trade["side"] == "long"
    # Quantity came from sizing, never from the model — EntryDecision has no size field.
    assert trade["qty"] > 0
    assert not hasattr(llm.data, "position_size")
    assert result["sizing"]["riskUsd"] <= result["sizing"]["riskBudgetUsd"] + 1e-9


async def test_small_account_trades_the_floor_on_borrowed_margin():
    """A $100 account cannot buy 0.001 BTC outright; leverage makes the floor reachable.

    This asserted a rejection before leverage bucketing existed. The trade must still be the
    exchange MINIMUM — leverage may make a position affordable, never larger.
    """
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None,
        account=_account(day_start_balance=100.0, free_balance=100.0))

    assert result["outcome"] == engine_mod.EXECUTED
    assert len(execution.trades) == 1
    assert execution.trades[0]["leverage"] > 1
    assert result["sizing"]["minSizeForced"] is True


async def test_account_too_small_for_the_daily_cap_places_no_order():
    """Below the point where a floor-sized loss fits the 2% day, nothing is sent."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None,
        account=_account(day_start_balance=23.0, free_balance=23.0))

    assert result["outcome"] == engine_mod.REJECTED
    assert execution.trades == []


async def test_exit_hold_leaves_the_position_open():
    from worker.pattern.decision.schemas import ExitDecision

    llm = FakeLlm(data=ExitDecision(action="HOLD", reasoning="just pausing"))
    execution = FakeExecution()
    state = FakeState(triggers=[trigger(name="regime_flip", side="short")])
    result = await _engine(llm, execution).decide(
        state, open_position=LONG_POSITION, account=_account())

    assert result["outcome"] == engine_mod.EXIT_HELD
    assert execution.closes == []


async def test_exit_executes_and_closes():
    from worker.pattern.decision.schemas import ExitDecision

    llm = FakeLlm(data=ExitDecision(action="EXIT", reasoning="structure broke"))
    execution = FakeExecution()
    state = FakeState(triggers=[trigger(name="regime_flip", side="short")])
    result = await _engine(llm, execution).decide(
        state, open_position=LONG_POSITION, account=_account())

    assert result["outcome"] == engine_mod.EXIT_EXECUTED
    assert execution.closes == [("p1", PRICE)]


async def test_unavailable_llm_on_an_exit_holds_rather_than_closing():
    """A protective stop is already in place, so holding is the conservative outcome."""
    from worker.pattern.decision.schemas import ExitDecision

    llm = FakeLlm(data=ExitDecision(action="EXIT", reasoning="x"), model="x-fallback")
    execution = FakeExecution()
    state = FakeState(triggers=[trigger(name="regime_flip", side="short")])
    result = await _engine(llm, execution).decide(
        state, open_position=LONG_POSITION, account=_account())

    assert result["outcome"] == engine_mod.EXIT_HELD
    assert execution.closes == []


# --------------------------------------------------------------------------- stale price
async def test_entry_is_refused_when_price_left_the_range_during_the_llm_call():
    """The 'setup moved on' guard must fire on the price at ORDER time, not the bar close.

    Previously the engine passed state.close into the validator — the same number the model built
    its entry range around — so the check compared a price against a range centred on itself and
    could never reject. The measured ~45s LLM latency is exactly when price runs away.
    """
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account(),
        price_now=lambda: PRICE + 400,   # ran well past entry_max while the model was thinking
    )

    assert result["outcome"] == engine_mod.REJECTED
    assert "moved on" in result["reason"]
    assert execution.trades == [], "nothing may be sent once the setup has moved on"


async def test_fill_uses_the_live_price_not_the_bar_close():
    """A paper fill at the stale close flatters every result; fill where we would really trade."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 200,
                                     entry_max=PRICE + 200, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    moved = PRICE + 120   # still inside the range, so the trade proceeds
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account(),
        price_now=lambda: moved,
    )

    assert result["outcome"] == engine_mod.EXECUTED
    assert execution.trades[0]["price"] == pytest.approx(moved)


async def test_bar_close_is_used_when_no_live_price_is_available():
    """A missing tick must not lose the decision — fall back, never raise."""
    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account(),
        price_now=lambda: None,
    )

    assert result["outcome"] == engine_mod.EXECUTED
    assert execution.trades[0]["price"] == pytest.approx(PRICE)


async def test_a_broken_price_source_still_trades_on_the_bar_close():
    """An exception reading the price must degrade, not discard the decision."""
    def boom():
        raise RuntimeError("tick cache unavailable")

    llm = FakeLlm(data=EntryDecision(action="ENTRY", side="long", entry_min=PRICE - 50,
                                     entry_max=PRICE + 50, stop_loss=PRICE * 0.99,
                                     take_profit=PRICE * 1.02, reasoning="x"))
    execution = FakeExecution()
    result = await _engine(llm, execution).decide(
        FakeState(triggers=[trigger()]), open_position=None, account=_account(),
        price_now=boom,
    )

    assert result["outcome"] == engine_mod.EXECUTED
    assert execution.trades[0]["price"] == pytest.approx(PRICE)
