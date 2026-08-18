"""Intent-detector tests.

The detector decides WHEN TO LOOK, never what to do, so the failures that matter are firing on
noise (which burns LLM budget and drags the model to worthless bars) and failing to fire on the
one case it exists for — a level crossed mid-bar.
"""
import pytest

from worker.pattern.intent import (
    COOLDOWN_MINUTES, CROSS_MARGIN_ATR, MOMENTUM_ATR, MOMENTUM_BARS,
    cooldown_ok, detect_intent,
)

ATR = 100.0
LEVELS = {"resistance": [{"price": 1000.0, "touches": 7}],
          "support": [{"price": 900.0, "touches": 5}]}


def test_a_level_crossed_mid_bar_is_the_case_this_exists_for():
    """Without this the 15m close would not notice for up to fourteen more minutes."""
    got = detect_intent([995.0, 1000.0 + ATR * CROSS_MARGIN_ATR + 1], LEVELS, ATR)
    assert got and got.kind == "level_cross" and got.side == "long"
    assert "broke resistance" in got.detail


def test_support_break_is_detected_as_a_short_intent():
    got = detect_intent([905.0, 900.0 - ATR * CROSS_MARGIN_ATR - 1], LEVELS, ATR)
    assert got and got.kind == "level_cross" and got.side == "short"


def test_a_wick_brushing_the_level_is_not_news():
    """Same margin the 15m trigger layer uses — one event, one threshold."""
    assert detect_intent([995.0, 1000.0 + 1.0], LEVELS, ATR) is None


def test_price_already_beyond_the_level_does_not_re_fire():
    """Fires on the CROSS, not for as long as price stays on the far side."""
    assert detect_intent([1050.0, 1060.0], LEVELS, ATR) is None


def test_momentum_needs_both_a_run_and_a_distance():
    step = ATR * MOMENTUM_ATR / MOMENTUM_BARS + 1
    rising = [500.0 + i * step for i in range(MOMENTUM_BARS + 1)]
    got = detect_intent(rising, {}, ATR)
    assert got and got.kind == "momentum" and got.side == "long"


def test_a_slow_drift_is_not_momentum():
    drift = [500.0 + i * 0.5 for i in range(MOMENTUM_BARS + 1)]
    assert detect_intent(drift, {}, ATR) is None


def test_a_non_monotonic_wobble_is_not_momentum():
    wobble = [500.0, 560.0, 540.0, 600.0]
    assert detect_intent(wobble, {}, ATR) is None


def test_falling_closes_give_a_short_intent():
    step = ATR * MOMENTUM_ATR / MOMENTUM_BARS + 1
    falling = [900.0 - i * step for i in range(MOMENTUM_BARS + 1)]
    got = detect_intent(falling, {}, ATR)
    assert got and got.side == "short"


def test_level_cross_outranks_momentum():
    """A level is concrete structure; a run is not. When both fire, report the level."""
    step = ATR * MOMENTUM_ATR / MOMENTUM_BARS + 1
    rising = [1000.0 - 3 * step + i * step for i in range(MOMENTUM_BARS + 1)]
    rising[-1] = 1000.0 + ATR * CROSS_MARGIN_ATR + 1
    got = detect_intent(rising, LEVELS, ATR)
    assert got.kind == "level_cross"


def test_no_atr_means_no_opinion():
    assert detect_intent([995.0, 1100.0], LEVELS, 0.0) is None


def test_too_little_history_is_safe():
    assert detect_intent([1000.0], LEVELS, ATR) is None
    assert detect_intent([], LEVELS, ATR) is None


def test_cooldown_blocks_a_second_wake_up_too_soon():
    """One volatile stretch must not wake the model on consecutive minutes."""
    t = 1_000_000_000_000
    assert cooldown_ok(t, None)
    assert not cooldown_ok(t, t - (COOLDOWN_MINUTES - 1) * 60_000)
    assert cooldown_ok(t, t - COOLDOWN_MINUTES * 60_000)
