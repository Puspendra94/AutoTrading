"""Sizing and guardrail tests — the money-critical half of the pattern brain.

These encode the two rules agreed with the operator:
  1. the day may lose at most 2% of its starting balance — a DAILY cap, with no limit on how
     many trades it takes to reach it;
  2. the stop may not sit more than 2% from entry.
"""
import pytest

from worker.exchange_info import SymbolFilters
from worker.pattern.decision.schemas import EntryDecision
from worker.pattern.decision.sizing import (
    DAILY_RISK_PCT, LEVERAGE_BUCKETS, MAX_MARGIN_PCT, MAX_STOP_PCT, MIN_STOP_ATR, MIN_STOP_PCT,
    RISK_FRACTION_OF_REMAINING, clamp_stop_pct, pick_leverage, size_position,
    take_profit_for, validate_risk_reward,
)
from worker.pattern.decision.validator import validate_entry

# BTCUSDT USD-M, as fetched from the live exchange.
BTC = SymbolFilters(symbol="BTCUSDT", step_size=0.001, min_qty=0.001, min_notional=50.0, tick_size=0.1)

PRICE = 63800.0
BALANCE = 10000.0
ATR_PCT = 0.00356  # 0.356% — the measured 15m value


def _size(**overrides):
    kwargs = dict(
        side="long", entry_price=PRICE, requested_stop_price=PRICE * (1 - 0.01),
        atr_pct=ATR_PCT, day_start_balance=BALANCE, realized_loss_today=0.0,
        free_balance=BALANCE, filters=BTC,
    )
    kwargs.update(overrides)
    return size_position(**kwargs)


# --------------------------------------------------------------------------- budget
def test_risk_per_trade_is_a_share_of_what_remains():
    """The only rule is the 2% DAILY cap; there is no trades-per-day constant."""
    r = _size()
    assert r.approved
    assert r.risk_budget_usd == pytest.approx(BALANCE * DAILY_RISK_PCT * RISK_FRACTION_OF_REMAINING)
    assert r.risk_budget_usd == pytest.approx(200.0 / 3)  # a third of 2% of 10k


def test_actual_risk_never_exceeds_the_budget():
    """Recomputed from the ROUNDED quantity, so the logged risk is what can really be lost."""
    r = _size()
    assert r.risk_usd <= r.risk_budget_usd + 1e-9


def test_losses_shrink_the_next_trade_without_a_trade_count():
    """Each loss shrinks the next position; the day ends on the 2% cap, not on a tally."""
    sizes = []
    lost = 0.0
    for _ in range(6):
        r = _size(realized_loss_today=lost)
        assert r.approved, "a fresh-ish budget must still allow a trade"
        sizes.append(r.risk_budget_usd)
        lost += r.risk_usd
    assert sizes == sorted(sizes, reverse=True), "risk per trade must taper as the day is spent"
    assert len(sizes) == 6, "no fixed trade count may cut the day short"


def test_day_stops_only_when_the_two_percent_is_gone():
    spent = _size(realized_loss_today=BALANCE * DAILY_RISK_PCT)
    assert not spent.approved
    assert "budget exhausted" in spent.reason


def test_partial_budget_shrinks_the_last_trade():
    """With only $2 of the day's allowance left, the trade may risk a third of it."""
    r = _size(realized_loss_today=BALANCE * DAILY_RISK_PCT - 2.0)
    assert r.approved
    assert r.risk_budget_usd == pytest.approx(2.0 * RISK_FRACTION_OF_REMAINING)


# --------------------------------------------------------------------------- leverage
def test_large_balance_never_borrows():
    """Leverage exists to reach the exchange floor, never to enlarge a position."""
    r = _size()
    assert r.approved
    assert r.leverage == 1
    assert r.min_size_forced is False
    assert r.margin_usd == pytest.approx(r.notional)


def test_small_balance_reaches_the_floor_on_leverage():
    """$75 cannot afford 0.001 BTC outright, but can at 2x — and the loss still fits the day."""
    r = _size(day_start_balance=75.0, free_balance=75.0)
    assert r.approved, r.reason
    assert r.min_size_forced is True
    assert r.leverage > 1
    assert r.quantity == pytest.approx(BTC.min_qty)
    # Borrowing frees margin; it does not change what a stop-out costs.
    assert r.risk_usd == pytest.approx(r.notional * r.stop_pct)
    assert r.margin_usd == pytest.approx(r.notional / r.leverage)
    assert r.margin_usd <= MAX_MARGIN_PCT * 75.0 + 1e-9


def test_floor_trade_is_refused_when_it_would_breach_the_daily_cap():
    """The guardrail is the operator's own 2% rule, not a separate invented ceiling."""
    r = _size(day_start_balance=23.0, free_balance=23.0)
    assert not r.approved
    assert "left of today's" in r.reason


def test_leverage_keeps_the_stop_clear_of_liquidation():
    """A bucket whose liquidation sits near the stop must never be chosen."""
    # A 2% stop needs liquidation >= 6% away, so 20x (5%) must be rejected outright.
    assert pick_leverage(notional=1000.0, stop_pct=0.02, balance=10.0) is None
    lev = pick_leverage(notional=100.0, stop_pct=0.02, balance=50.0)
    assert lev in LEVERAGE_BUCKETS and (1.0 / lev) >= 0.02 * 3.0


# --------------------------------------------------------------------------- stop clamping
def test_stop_is_clamped_to_the_two_percent_ceiling():
    """Rule 2: 'SL must not exceed 2% of the invested amount' is a stop-DISTANCE cap."""
    r = _size(requested_stop_price=PRICE * (1 - 0.08))  # an 8% stop
    assert r.approved
    assert r.stop_pct == pytest.approx(MAX_STOP_PCT)
    assert r.clamped is True
    assert r.stop_price == pytest.approx(PRICE * (1 - MAX_STOP_PCT))


def test_hair_tight_stop_is_floored_rather_than_exploding_the_position():
    """A $10 stop would imply an enormous notional; the floor is what prevents that."""
    r = _size(requested_stop_price=PRICE - 10)
    assert r.approved
    assert r.stop_pct >= MIN_STOP_PCT
    assert r.stop_pct >= MIN_STOP_ATR * ATR_PCT  # and outside the noise band, not just inside policy
    assert r.clamped is True


def test_the_volatility_floor_actually_binds():
    """The regression the first paper week exposed.

    At MIN_STOP_ATR=0.5 this floor was unreachable — 0.5 ATR was always smaller than the fixed
    0.3% floor, so stop placement had no relationship to volatility at all and every stop landed
    around 2 ATR, inside one bar's normal range. The ATR term must be the binding one.
    """
    r = _size(requested_stop_price=PRICE * (1 - 0.004))  # 0.4%: above MIN_STOP_PCT, ~1.1 ATR
    assert r.clamped is True
    assert r.stop_pct == pytest.approx(MIN_STOP_ATR * ATR_PCT)
    assert r.stop_pct > MIN_STOP_PCT


def test_a_wider_stop_shrinks_the_position_rather_than_the_risk():
    """Why widening the floor is free: dollars risked are unchanged, size absorbs it."""
    tight = _size(requested_stop_price=PRICE * (1 - 0.011))
    wide = _size(requested_stop_price=PRICE * (1 - 0.018))
    assert wide.notional < tight.notional
    assert wide.risk_usd == pytest.approx(tight.risk_usd, rel=0.01)


def test_stop_floor_scales_with_volatility():
    """A fixed percentage floor would be too tight in a fast market and too wide in a quiet one."""
    quiet, _ = clamp_stop_pct(0.0001, atr_pct=0.001)
    volatile, _ = clamp_stop_pct(0.0001, atr_pct=0.02)
    assert volatile > quiet


def test_reasonable_stop_is_left_alone():
    # 1.5% is comfortably outside both floors (3 ATR = 1.068% here) and inside the 2% ceiling.
    r = _size(requested_stop_price=PRICE * (1 - 0.015))
    assert r.clamped is False
    assert r.stop_pct == pytest.approx(0.015)


# --------------------------------------------------------------------------- caps
def test_notional_is_capped_and_under_risks_rather_than_over_risks():
    """A tight stop wants a huge notional; the cap binds and the trade risks LESS than budget."""
    r = _size(requested_stop_price=PRICE * (1 - 0.004))
    assert r.approved
    assert r.notional <= MAX_MARGIN_PCT * BALANCE + 1e-6
    assert r.risk_usd < r.risk_budget_usd  # capped -> under-risked, the safe direction


def test_quantity_respects_exchange_step_size():
    r = _size()
    steps = r.quantity / BTC.step_size
    assert abs(steps - round(steps)) < 1e-6, f"{r.quantity} is not a multiple of {BTC.step_size}"


def test_rounding_is_down_never_up():
    filters = SymbolFilters("X", step_size=0.01, min_qty=0.001, min_notional=0.0, tick_size=0.1)
    assert filters.round_quantity(0.0199) == pytest.approx(0.01)
    assert filters.round_quantity(0.999) == pytest.approx(0.99)


# --------------------------------------------------------------------------- account floor
def test_modest_account_reaches_the_floor_instead_of_being_locked_out():
    """$100 cannot buy 0.001 BTC outright, but 2x margin fits and the risk stays inside the day.

    This used to be a rejection. Leverage is what turned "cannot trade at all" into "can trade
    the exchange minimum", which is the whole point of the bucket selection.
    """
    r = _size(day_start_balance=100.0, free_balance=100.0)
    assert r.approved, r.reason
    assert r.min_size_forced is True
    assert r.leverage == 2
    assert r.margin_usd == pytest.approx(r.notional / 2)
    assert r.risk_usd <= r.remaining_today_usd


def test_account_too_small_even_for_leverage_is_refused_clearly():
    """Below the point where a floor-sized loss fits the daily cap, no leverage can rescue it."""
    r = _size(day_start_balance=23.0, free_balance=23.0)
    assert not r.approved
    assert "smallest order this exchange accepts" in r.reason


def test_short_sizing_mirrors_long():
    r = _size(side="short", requested_stop_price=PRICE * 1.015)
    assert r.approved
    assert r.stop_price > PRICE
    assert r.stop_pct == pytest.approx(0.015)


# --------------------------------------------------------------------------- reward:risk
def test_take_profit_matches_the_requested_reward_ratio():
    tp = take_profit_for("long", 100.0, 98.0, 1.5)
    assert tp == pytest.approx(103.0)  # 2.0 risk * 1.5
    ok, rr, _ = validate_risk_reward("long", 100.0, 98.0, tp, 1.5)
    assert ok and rr == pytest.approx(1.5)


def test_target_on_the_wrong_side_is_rejected():
    ok, _, why = validate_risk_reward("long", 100.0, 98.0, 97.0, 1.5)
    assert not ok and "wrong side" in why


# --------------------------------------------------------------------------- validator
def _entry(**overrides):
    payload = dict(
        action="ENTRY", side="long", entry_min=PRICE - 50, entry_max=PRICE + 50,
        stop_loss=PRICE * 0.99, take_profit=PRICE * 1.02, confidence=0.7, reasoning="test",
    )
    payload.update(overrides)
    return EntryDecision(**payload)


def test_valid_entry_passes():
    v = validate_entry(_entry(), current_price=PRICE, regime_label="uptrend")
    assert v.ok, v.reason


def test_fading_a_strong_trend_is_rejected():
    v = validate_entry(_entry(), current_price=PRICE, regime_label="strong_downtrend")
    assert not v.ok and "fading a strong trend" in v.reason


def test_entry_against_a_WEAK_trend_is_allowed():
    """Only STRONG trends are off limits. Fading a weak trend or trading a range edge is a
    judgement call that belongs to the model, not a deterministic refusal."""
    assert validate_entry(_entry(), current_price=PRICE, regime_label="downtrend").ok
    assert validate_entry(_entry(), current_price=PRICE, regime_label="range").ok


def test_entry_outside_its_own_range_is_rejected():
    """Price moved past the model's range between decision and fill."""
    v = validate_entry(_entry(entry_min=PRICE + 100, entry_max=PRICE + 200),
                       current_price=PRICE, regime_label="uptrend")
    assert not v.ok and "outside the proposed entry range" in v.reason


def test_stop_on_the_wrong_side_is_rejected():
    v = validate_entry(_entry(stop_loss=PRICE * 1.01), current_price=PRICE, regime_label="uptrend")
    assert not v.ok and "not below entry" in v.reason


def test_thin_reward_risk_is_rejected():
    v = validate_entry(_entry(take_profit=PRICE * 1.005), current_price=PRICE, regime_label="uptrend")
    assert not v.ok and "Reward:risk" in v.reason


def test_incomplete_entry_is_rejected_not_defaulted():
    """A missing stop must never be invented — it is the most important number in the trade."""
    v = validate_entry(_entry(stop_loss=None), current_price=PRICE, regime_label="uptrend")
    assert not v.ok and "missing required field" in v.reason


def test_entry_without_a_side_is_rejected():
    v = validate_entry(_entry(side=None), current_price=PRICE, regime_label="uptrend")
    assert not v.ok and "valid side" in v.reason


# --------------------------------------------------------------------------- exchange filters
async def test_empty_symbol_is_reported_rather_than_silently_falling_back():
    """Regression: the executor read `symbol` from the risk-gate store, which does not return it.
    An empty symbol failed the exchange lookup, and because the offline fallback happens to match
    BTCUSDT futures, the wrong venue's rules would have gone unnoticed."""
    from worker.exchange_info import FALLBACK, get_symbol_filters

    assert await get_symbol_filters("") is FALLBACK


def test_futures_and_spot_filters_are_cached_separately():
    """A futures ticker must not be sized against spot rules — different minNotional and step."""
    from worker import exchange_info

    exchange_info.clear_cache()
    exchange_info._cache[("BTCUSDT", True)] = SymbolFilters("BTCUSDT", 0.001, 0.001, 50.0, 0.1)
    exchange_info._cache[("BTCUSDT", False)] = SymbolFilters("BTCUSDT", 0.00001, 0.00001, 5.0, 0.01)

    assert exchange_info._cache[("BTCUSDT", True)].min_notional == 50.0
    assert exchange_info._cache[("BTCUSDT", False)].min_notional == 5.0
    exchange_info.clear_cache()
