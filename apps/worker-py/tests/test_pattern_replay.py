"""Replay-harness tests.

The harness exists to be trusted, so the things worth testing are the ones that would quietly
produce a WRONG answer rather than an error: bucket alignment, look-ahead, forward-return signing,
and whether the ladder it runs is really the production one.
"""
import math

import pytest

from worker.pattern.exits import MAX_HOLD_BARS
from worker.pattern.replay import (
    FORWARD_HORIZONS, LONG, SHORT, NOTIONAL_USD, Observation, ReplayResult,
    _agg, _atr_for_path, _stop_and_target, _t, bucket_ms, replay, report, rollup,
    simulate_ladder,
)

MIN = 60_000


def bars_1m(n, start_ms=0, price=60_000.0, drift=0.0, wiggle=0.0):
    """A deterministic 1m series. `drift` is per-bar fractional change."""
    out, p = [], price
    for i in range(n):
        p *= (1 + drift)
        hi = p * (1 + wiggle)
        lo = p * (1 - wiggle)
        out.append({"timestamp": start_ms + i * MIN, "open": p, "high": hi, "low": lo,
                    "close": p, "volume": 1.0})
    return out


# --------------------------------------------------------------------------- rollup
def test_bucket_ms_matches_the_interval_strings_used_live():
    assert bucket_ms("1m") == MIN
    assert bucket_ms("15m") == 15 * MIN
    assert bucket_ms("4h") == 4 * 60 * MIN
    assert bucket_ms("1d") == 24 * 60 * MIN


def test_rollup_aggregates_ohlcv_correctly():
    raw = [
        {"timestamp": 0, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 1},
        {"timestamp": MIN, "open": 11, "high": 15, "low": 8, "close": 14, "volume": 2},
        {"timestamp": 2 * MIN, "open": 14, "high": 14, "low": 13, "close": 13, "volume": 3},
    ]
    [bar] = rollup(raw, "3m")
    assert bar["open"] == 10 and bar["close"] == 13     # first open, last close
    assert bar["high"] == 15 and bar["low"] == 8        # max high, min low
    assert bar["volume"] == 6


def test_rollup_buckets_are_epoch_aligned_like_time_bucket():
    """TimescaleDB floors to the epoch. A harness that aligned to the first bar instead would
    replay different candles than the live engine sees, and every result would be off."""
    start = 7 * MIN                                    # deliberately not on a 15m boundary
    out = rollup(bars_1m(30, start_ms=start), "15m")
    assert [b["timestamp"] for b in out][:2] == [0, 15 * MIN]


def test_rollup_drops_a_partial_trailing_bucket():
    """The live engine never evaluates an incomplete bar; nor may the replay."""
    assert len(rollup(bars_1m(15), "15m")) == 1        # exactly one full bucket
    assert len(rollup(bars_1m(20), "15m")) == 1        # 5 extra minutes -> still one


# --------------------------------------------------------------------------- ladder
def _flat_atr(path, atr=100.0):
    return [atr] * len(path)


def test_ladder_stops_out_and_the_loss_is_about_one_R():
    entry, stop = 60_000.0, 59_400.0                   # 1% stop
    path = bars_1m(50, price=entry, drift=-0.0005)     # walks steadily down through the stop
    r = simulate_ladder(side=LONG, entry=entry, stop=stop, target=61_200.0,
                        quantity=NOTIONAL_USD / entry, path=path,
                        atr_at=_flat_atr(path), interval="15m")
    assert r.exit_reason == "stop"
    assert r.r < 0 and r.r == pytest.approx(-1.0, abs=0.15)   # ~1R, slightly worse for costs
    assert r.pnl_usd < 0


def test_ladder_lets_a_winner_run_past_its_target():
    """The whole design point: crossing the target ratchets, it does not close."""
    entry = 60_000.0
    path = bars_1m(400, price=entry, drift=0.0004)     # a long, steady climb
    r = simulate_ladder(side=LONG, entry=entry, stop=entry * 0.99, target=entry * 1.015,
                        quantity=NOTIONAL_USD / entry, path=path,
                        atr_at=_flat_atr(path), interval="15m")
    assert r.r > 1.5                                   # ran well beyond the 1.5R target
    assert r.pnl_usd > 0


def test_ladder_charges_fees_so_a_flat_trade_loses():
    entry = 60_000.0
    path = bars_1m(30, price=entry)                    # price never moves
    r = simulate_ladder(side=LONG, entry=entry, stop=entry * 0.99, target=entry * 1.015,
                        quantity=NOTIONAL_USD / entry, path=path,
                        atr_at=_flat_atr(path), interval="15m")
    assert r.pnl_usd < 0 and r.fees_usd > 0


def test_ladder_respects_max_hold():
    entry = 60_000.0
    # Enough 1m bars to exceed MAX_HOLD_BARS of 15m bars, going nowhere.
    path = bars_1m(MAX_HOLD_BARS * 15 + 30, price=entry)
    r = simulate_ladder(side=LONG, entry=entry, stop=entry * 0.9, target=entry * 1.9,
                        quantity=NOTIONAL_USD / entry, path=path,
                        atr_at=_flat_atr(path), interval="15m")
    assert r.exit_reason == "max_hold"


def test_a_short_mirrors_a_long():
    entry = 60_000.0
    down = bars_1m(200, price=entry, drift=-0.0004)
    r = simulate_ladder(side=SHORT, entry=entry, stop=entry * 1.01, target=entry * 0.985,
                        quantity=NOTIONAL_USD / entry, path=down,
                        atr_at=_flat_atr(down), interval="15m")
    assert r.r > 0 and r.pnl_usd > 0                   # a short profits as price falls


def test_an_unresolved_trade_is_marked_truncated_not_counted_flat():
    """Running out of data must not look like a scratch trade — it would drag expectancy to 0."""
    entry = 60_000.0
    path = bars_1m(5, price=entry)                     # nowhere near resolving
    r = simulate_ladder(side=LONG, entry=entry, stop=entry * 0.9, target=entry * 1.9,
                        quantity=NOTIONAL_USD / entry, path=path,
                        atr_at=_flat_atr(path), interval="15m")
    assert r.exit_reason == "truncated"


# --------------------------------------------------------------------------- protective levels
def test_stop_uses_the_policy_atr_floor():
    atr_pct = 0.004
    stop, target = _stop_and_target(LONG, 60_000.0, atr_pct)
    from worker.pattern.decision.sizing import MIN_STOP_ATR
    assert (60_000.0 - stop) / 60_000.0 == pytest.approx(MIN_STOP_ATR * atr_pct)
    assert target > 60_000.0 and stop < 60_000.0


def test_short_levels_are_inverted():
    stop, target = _stop_and_target(SHORT, 60_000.0, 0.004)
    assert stop > 60_000.0 and target < 60_000.0


# --------------------------------------------------------------------------- ATR alignment
def test_atr_for_path_uses_only_completed_bars():
    """Look-ahead check: while inside bar i's successor, the ATR in force is still bar i's."""
    step = 15 * MIN
    bars = [{"timestamp": k * step, "close": 100.0} for k in range(5)]
    atr_by_bar = [1.0, 2.0, 3.0, 4.0, 5.0]
    # Path covering the 15 minutes after bar 1 closes, then into the next bar.
    path = [{"timestamp": 2 * step + m * MIN} for m in range(0, 20)]
    out = _atr_for_path(path, bars, atr_by_bar, decision_idx=1, step=step)
    assert out[0] == 2.0                       # bar 1 has completed, bar 2 has not
    assert out[-1] == 3.0                      # bar 2 completed 5 minutes ago
    assert max(out) <= 3.0                     # never reaches bar 3's or 4's ATR


# --------------------------------------------------------------------------- aggregation
def _obs(cohort="signal", r=0.5, fwd=1.0, exit_reason="stop", **kw):
    o = Observation(bar_time_ms=0, cohort=cohort, trigger="t", side=LONG, regime="range",
                    adx=20.0, atr_pct=0.003, close=60_000.0, **kw)
    o.r, o.pnl_usd, o.exit_reason = r, r * 100, exit_reason
    o.forward = {h: fwd for h in FORWARD_HORIZONS}
    return o


def test_an_unsimulated_bar_is_not_counted_as_a_flat_trade():
    """'no_path' is the default, so a bar too near the end of the data can never be silently
    aggregated as a scratch result and drag expectancy toward zero."""
    a = _agg([_obs(r=1.0), Observation(bar_time_ms=0, cohort="signal", trigger="t", side=LONG,
                                       regime="range", adx=20.0, atr_pct=0.003, close=60_000.0)])
    assert a["n"] == 2 and a["n_resolved"] == 1
    assert a["expectancy_r"] == pytest.approx(1.0)


def test_truncated_trades_are_excluded_from_expectancy():
    a = _agg([_obs(r=1.0), _obs(r=1.0), _obs(r=-99.0, exit_reason="truncated")])
    assert a["n"] == 3 and a["n_resolved"] == 2
    assert a["expectancy_r"] == pytest.approx(1.0)


def test_a_trailing_exit_is_not_reported_as_a_stop_out():
    """Both come back from the ladder as 'Stop-loss hit at ...'; only the lifecycle separates a
    winner being banked from the trade being wrong."""
    from worker.pattern.replay import _reason_kind
    assert _reason_kind("Stop-loss hit at 100.00 (stop 99.00).", "open") == "stop"
    assert _reason_kind("Stop-loss hit at 100.00 (stop 99.00).", "runner") == "trail_exit"
    assert _reason_kind("Max hold reached (96 bars).", "open") == "max_hold"


def test_gate_skip_reasons_group_by_kind_not_by_wording():
    """Every refusal quotes its own numbers, so keying on the raw string produced one bucket per
    distinct ADX reading and buried the real distribution."""
    from worker.pattern.replay import _skip_key
    assert _skip_key("ADX 40.6 — the trend is already extended (>= 40)") == \
        _skip_key("ADX 51.2 — the trend is already extended (>= 40)")
    assert _skip_key("Cooldown: 1 of 2 bars since the last close.") == \
        _skip_key("Cooldown: 0 of 2 bars since the last close.")
    assert _skip_key("No trigger on this bar") != _skip_key("ADX 40.6 — the trend is extended")


def test_welch_t_separates_a_real_difference_from_a_wash():
    from worker.pattern.replay import _welch_t
    same = [0.1, -0.1] * 100
    assert abs(_welch_t(same, list(reversed(same)))) < 2
    better = [0.5, 0.6, 0.4] * 70
    worse = [-0.5, -0.6, -0.4] * 70
    assert _welch_t(better, worse) > 2


def test_target_rate_counts_only_trades_that_reached_the_runner():
    hit = _obs(r=2.0, exit_reason="trail_exit")
    hit.reached_target = True
    a = _agg([hit, _obs(r=-1.0), _obs(r=-1.0), _obs(r=-1.0)])
    assert a["target_rate"] == pytest.approx(25.0)


def test_t_stat_flags_noise_and_signal():
    assert math.isnan(_t([1.0]))
    assert abs(_t([0.01, -0.01] * 50)) < 2          # a wash
    assert abs(_t([1.0, 1.1, 0.9] * 40)) > 2        # consistent


def test_profit_factor_and_win_rate():
    a = _agg([_obs(r=1.0), _obs(r=1.0), _obs(r=-1.0)])
    assert a["win_rate"] == pytest.approx(66.666, abs=0.01)
    assert a["profit_factor"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- end to end
def test_forward_returns_are_signed_by_the_traded_side():
    """A short in a falling market must show a POSITIVE forward return — the sign convention is
    'was this direction right', not 'did price go up'."""
    from worker.pattern.replay import _measure
    step = 15 * MIN
    bars = [{"timestamp": k * step, "close": 100.0 - k, "open": 100.0, "high": 100.0,
             "low": 100.0, "volume": 1.0} for k in range(200)]
    ctx = {"regime": "downtrend", "adx": 30.0, "atr_pct": 0.01, "close": bars[10]["close"]}
    short = _measure(bars, [], {}, [0.0] * 200, 10, ctx, SHORT, "t", "signal", "15m", step)
    long_ = _measure(bars, [], {}, [0.0] * 200, 10, ctx, LONG, "t", "signal", "15m", step)
    assert short.forward[FORWARD_HORIZONS[0]] > 0
    assert long_.forward[FORWARD_HORIZONS[0]] < 0


def test_replay_runs_end_to_end_and_reports():
    """A full pass over synthetic data: it must produce a state, a gate decision per bar, and a
    report — without needing a database."""
    import random
    rng = random.Random(1)
    price, raw = 60_000.0, []
    for i in range(60 * 24 * 20):                 # 20 days of 1m bars
        price *= (1 + rng.gauss(0, 0.0004))
        raw.append({"timestamp": i * MIN, "open": price, "high": price * 1.0006,
                    "low": price * 0.9994, "close": price, "volume": 10.0})

    result = replay(raw, symbol="BTCUSDT", interval="15m", candle_limit=300,
                    mode="signals", random_baseline=20, progress_every=0)
    assert isinstance(result, ReplayResult)
    assert result.bars_evaluated > 0
    # Every evaluated bar is accounted for: either it reached the gate or it was skipped.
    assert result.gate_calls + sum(result.gate_skips.values()) == result.bars_evaluated
    text = report(result)
    assert "REPLAY" in text and "VERDICT" in text


def test_replay_refuses_a_window_shorter_than_the_warmup():
    result = replay(bars_1m(60 * 24), interval="15m", candle_limit=500, progress_every=0)
    assert result.bars_evaluated == 0 and result.observations == []


def test_sequential_mode_takes_far_fewer_trades_than_signals_mode():
    """Sequential holds one position at a time, so it must sample a strict subset."""
    import random
    rng = random.Random(2)
    price, raw = 60_000.0, []
    for i in range(60 * 24 * 20):
        price *= (1 + rng.gauss(0, 0.0004))
        raw.append({"timestamp": i * MIN, "open": price, "high": price * 1.0006,
                    "low": price * 0.9994, "close": price, "volume": 10.0})

    sig = replay(raw, interval="15m", candle_limit=300, mode="signals",
                 random_baseline=0, progress_every=0)
    seq = replay(raw, interval="15m", candle_limit=300, mode="sequential",
                 random_baseline=0, progress_every=0)
    assert seq.gate_calls <= sig.gate_calls
    # signals mode also measures the inverse of every signal; sequential does not.
    assert all(o.cohort == "signal" for o in seq.observations)


# --------------------------------------------------------------------------- verdict wording
def _stats(n=500, mean=0.0, spread=0.5):
    """An _agg-shaped dict with a controllable mean."""
    from worker.pattern.replay import _agg
    rows = []
    for i in range(n):
        rows.append(_obs(r=mean + (spread if i % 2 else -spread)))
    a = _agg(rows)
    return a


def test_verdict_reports_the_overlap_caveat_only_in_signals_mode():
    from worker.pattern.replay import _verdict
    sig, inv, rnd = _stats(), _stats(mean=-0.05), _stats(mean=-0.07)
    assert "OVERLAP" in _verdict(sig, inv, rnd, mode="signals")
    assert "OVERLAP" not in _verdict(sig, inv, rnd, mode="sequential")
    assert "do not overlap" in _verdict(sig, inv, rnd, mode="sequential")


def test_verdict_says_directional_edge_is_unmeasured_without_an_inverse_cohort():
    """Sequential mode has no inverse cohort — that must be stated, not silently omitted."""
    from worker.pattern.replay import _verdict
    empty = _agg([])
    text = _verdict(_stats(), empty, _stats(mean=-0.07), mode="sequential")
    assert "not measured" in text


def test_verdict_refuses_to_conclude_on_a_tiny_sample():
    from worker.pattern.replay import _verdict
    text = _verdict(_stats(n=20), _stats(n=20), _stats(n=20))
    assert "too few to conclude" in text


def test_verdict_separates_profitability_from_directional_edge():
    """The useful case: a real edge that is smaller than the fees. That must NOT read the same as
    'the triggers are noise' — the two call for opposite responses."""
    from worker.pattern.replay import _verdict
    # Signal clearly beats its inverse, but sits at break-even after costs.
    sig = _stats(n=2000, mean=0.0, spread=0.4)
    inv = _stats(n=2000, mean=-0.35, spread=0.4)
    text = _verdict(sig, inv, _stats(mean=-0.5))
    assert "Real directional edge" in text
    assert "smaller than the fees" in text
