"""Exit-ladder tests.

The operator's requirement was "the stop must always execute, but don't close on the target — let
profit run". These tests pin the part that makes that safe: crossing the target moves protection
rather than closing, and protection can only ever improve.
"""
import pytest

from worker.pattern.exits import (
    FEE_BUFFER_PCT, MAX_HOLD_BARS, OPEN, RUNNER, TRAIL_ATR_MULTIPLE,
    better_extreme, breakeven_stop, evaluate_exit, is_better_stop, r_multiple, trailing_stop,
)

ENTRY = 60000.0
ATR = 200.0


def pos(**over):
    base = dict(side="long", entryPrice=ENTRY, stopLoss=ENTRY - 600, takeProfit=ENTRY + 900,
                initialStop=ENTRY - 600, lifecycle=OPEN, extremePrice=ENTRY)
    base.update(over)
    return base


# --------------------------------------------------------------------------- stop is absolute
def test_stop_closes_the_position():
    a = evaluate_exit(pos(), price=ENTRY - 600, atr=ATR)
    assert a.is_exit and "Stop-loss hit" in a.reason


def test_stop_is_checked_before_anything_else():
    """A bar that gaps through the stop AND past the target must stop out, not start running."""
    a = evaluate_exit(pos(stopLoss=ENTRY + 1000, takeProfit=ENTRY + 900, side="short"),
                      price=ENTRY + 1200, atr=ATR)
    assert a.is_exit


def test_short_stop_is_above_entry():
    p = pos(side="short", stopLoss=ENTRY + 600, takeProfit=ENTRY - 900, initialStop=ENTRY + 600)
    assert evaluate_exit(p, price=ENTRY + 600, atr=ATR).is_exit
    assert not evaluate_exit(p, price=ENTRY - 100, atr=ATR).is_exit


def test_max_hold_closes_a_stagnant_position():
    a = evaluate_exit(pos(), price=ENTRY + 10, atr=ATR, bars_held=MAX_HOLD_BARS)
    assert a.is_exit and "Max hold" in a.reason


# --------------------------------------------------------------------------- target is a handoff
def test_target_does_not_close_the_position():
    """The whole point: crossing the target lets profit run instead of booking it."""
    a = evaluate_exit(pos(), price=ENTRY + 900, atr=ATR)
    assert not a.is_exit
    assert a.is_ratchet
    assert a.new_lifecycle == RUNNER


def test_crossing_the_target_protects_at_least_breakeven_plus_fees():
    """A winner must not be able to become a loser after tagging its target."""
    a = evaluate_exit(pos(), price=ENTRY + 900, atr=ATR)
    assert a.new_stop >= ENTRY * (1 + FEE_BUFFER_PCT) - 1e-9
    assert a.new_stop > ENTRY  # strictly profitable, fees covered


def test_fast_move_takes_breakeven_over_a_lower_trail():
    """Early in a sharp move the ATR trail can still sit BELOW entry; accepting it would leave the
    trade able to close for a loss after reaching its target."""
    a = evaluate_exit(pos(), price=ENTRY + 900, atr=1000.0)  # 2 ATR = 2000 below the extreme
    assert a.new_stop == pytest.approx(breakeven_stop("long", ENTRY))


def test_short_target_handoff_mirrors_long():
    p = pos(side="short", stopLoss=ENTRY + 600, takeProfit=ENTRY - 900,
            initialStop=ENTRY + 600, extremePrice=ENTRY)
    a = evaluate_exit(p, price=ENTRY - 900, atr=ATR)
    assert a.is_ratchet and a.new_lifecycle == RUNNER
    assert a.new_stop < ENTRY  # protected below entry for a short


# --------------------------------------------------------------------------- trailing
def test_runner_trails_behind_the_best_price():
    p = pos(lifecycle=RUNNER, stopLoss=ENTRY, extremePrice=ENTRY + 2000)
    a = evaluate_exit(p, price=ENTRY + 2000, atr=ATR)
    assert a.is_ratchet
    assert a.new_stop == pytest.approx(ENTRY + 2000 - ATR * TRAIL_ATR_MULTIPLE)


def test_trail_never_widens():
    """Ratchet-only. A stop that can move away from profit is not a stop.

    The stop already sits at +1700 while the ATR trail behind the +2000 extreme computes to
    +1600 — lower. Holding the tighter existing stop is the required behaviour; adopting the
    looser candidate would hand back protection the trade already earned.
    """
    p = pos(lifecycle=RUNNER, stopLoss=ENTRY + 1700, extremePrice=ENTRY + 2000)
    a = evaluate_exit(p, price=ENTRY + 1750, atr=ATR)  # 2 ATR behind 2000 = +1600 < +1700
    assert a.action == "hold"


def test_pullback_inside_the_trail_does_not_exit():
    p = pos(lifecycle=RUNNER, stopLoss=ENTRY + 1500, extremePrice=ENTRY + 2000)
    assert not evaluate_exit(p, price=ENTRY + 1700, atr=ATR).is_exit


def test_pullback_through_the_trail_exits():
    p = pos(lifecycle=RUNNER, stopLoss=ENTRY + 1600, extremePrice=ENTRY + 2000)
    assert evaluate_exit(p, price=ENTRY + 1600, atr=ATR).is_exit


# --------------------------------------------------------------------------- helpers
def test_extreme_tracks_the_favourable_direction_only():
    assert better_extreme("long", 100.0, 120.0) == 120.0
    assert better_extreme("long", 100.0, 80.0) == 100.0
    assert better_extreme("short", 100.0, 80.0) == 80.0
    assert better_extreme("short", 100.0, 120.0) == 100.0
    assert better_extreme("long", None, 55.0) == 55.0


def test_is_better_stop_direction_awareness():
    assert is_better_stop("long", 105.0, 100.0)
    assert not is_better_stop("long", 95.0, 100.0)
    assert is_better_stop("short", 95.0, 100.0)
    assert not is_better_stop("short", 105.0, 100.0)
    assert is_better_stop("long", 1.0, None)


def test_trailing_stop_sits_below_for_long_and_above_for_short():
    assert trailing_stop("long", 1000.0, 10.0, 2.0) == 980.0
    assert trailing_stop("short", 1000.0, 10.0, 2.0) == 1020.0


# --------------------------------------------------------------------------- R multiple
def test_r_multiple_measures_against_the_ORIGINAL_risk():
    """A runner that trailed to +3R did not make 0R just because its stop reached breakeven."""
    p = pos(stopLoss=ENTRY + 100, initialStop=ENTRY - 600)  # stop has ratcheted well up
    assert r_multiple(p, ENTRY + 1800) == pytest.approx(3.0)


def test_r_multiple_is_negative_on_a_loss():
    assert r_multiple(pos(), ENTRY - 600) == pytest.approx(-1.0)


def test_r_multiple_handles_a_zero_risk_position():
    assert r_multiple(pos(initialStop=ENTRY), ENTRY + 100) == 0.0
