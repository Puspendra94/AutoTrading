"""Feature-screener tests.

The failure mode that matters here is a screener that reports edge where there is none — via
look-ahead, or by correlating a feature with itself. A wrong answer sends you off building on a
feature that never predicted anything, so these tests are mostly about the null case being null.
"""
import math

import numpy as np
import pytest

from worker.pattern.screen_features import (
    HORIZONS, build_features, forward_returns, report, screen, spearman, _roll_mean, _zscore,
)

MIN = 60_000


def bars_from(closes, buy_shares=None, seed=0):
    """Synthetic bars; buy_shares drives taker_buy_base so flow features can be controlled."""
    out = []
    for i, c in enumerate(closes):
        vol = 100.0
        bs = 0.5 if buy_shares is None else buy_shares[i]
        out.append({"timestamp": i * 15 * MIN, "open": c, "high": c * 1.001, "low": c * 0.999,
                    "close": c, "volume": vol, "taker_buy_base": vol * bs, "trades": 50})
    return out


# --------------------------------------------------------------------------- no look-ahead
def test_forward_return_is_strictly_after_the_bar():
    bars = bars_from([100.0, 110.0, 121.0])
    f = forward_returns(bars, 1)
    assert f[0] == pytest.approx(0.10)      # bar 0 -> bar 1
    assert f[1] == pytest.approx(0.10)      # bar 1 -> bar 2
    assert math.isnan(f[2])                 # nothing after the last bar


def test_a_feature_of_past_bars_cannot_see_the_future():
    """The null that must hold: on a random walk, no backward-looking feature should score."""
    rng = np.random.default_rng(5)
    closes = 60_000 * np.exp(np.cumsum(rng.normal(0, 0.002, 4000)))
    shares = rng.uniform(0.3, 0.7, 4000)
    rows = screen(bars_from(list(closes), list(shares)), horizons=(1,))
    for r in rows:
        if not math.isnan(r["ic1"]):
            assert abs(r["ic1"]) < 0.08, f"{r['feature']} scored {r['ic1']} on a random walk"


def test_a_planted_signal_is_detected():
    """Sanity in the other direction — if flow really does lead price, the screen must see it."""
    rng = np.random.default_rng(7)
    n = 4000
    shares = rng.uniform(0.2, 0.8, n)
    closes = [60_000.0]
    for i in range(1, n):
        # Next bar's return is driven by THIS bar's buy share, plus noise.
        drift = (shares[i - 1] - 0.5) * 0.01
        closes.append(closes[-1] * (1 + drift + rng.normal(0, 0.0005)))
    rows = {r["feature"]: r for r in screen(bars_from(closes, list(shares)), horizons=(1,))}
    assert rows["flow:buy_share"]["ic1"] > 0.3
    assert rows["flow:buy_share"]["t1"] > 5


# --------------------------------------------------------------------------- helpers
def test_rolling_mean_uses_only_past_values():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    m = _roll_mean(x, 2)
    assert math.isnan(m[0])
    assert m[1] == pytest.approx(1.5)
    assert m[3] == pytest.approx(3.5)


def test_zscore_is_nan_before_the_window_fills():
    z = _zscore(np.arange(10, dtype=float), 5)
    assert math.isnan(z[0]) and math.isnan(z[3])
    assert not math.isnan(z[9])


def test_spearman_is_rank_based_and_outlier_resistant():
    a = np.arange(500, dtype=float)
    b = a.copy(); b[0] = 1e12          # one absurd outlier
    ic, n = spearman(a, b)
    assert n == 500
    assert ic > 0.9                     # a Pearson correlation would be wrecked by that point


def test_spearman_refuses_a_tiny_sample():
    ic, n = spearman(np.arange(10, dtype=float), np.arange(10, dtype=float))
    assert math.isnan(ic) and n == 10


def test_missing_flow_data_is_nan_not_zero():
    """NULL taker_buy_base must not read as 'zero aggressive buying' — that is a strong signal
    and a completely fabricated one."""
    bars = bars_from([100.0] * 50)
    for b in bars:
        b["taker_buy_base"] = None
    f = build_features(bars)
    assert np.all(np.isnan(f["flow:buy_share"]))


def test_report_renders_and_ranks_by_strongest_ic():
    rows = screen(bars_from([100.0 * (1.001 ** i) for i in range(500)]), horizons=(1,))
    text = report(rows, "15m", horizons=(1,))
    assert "FEATURE SCREEN" in text and "flow:" in text


def test_roll_mean_of_an_all_nan_window_is_nan_not_zero():
    """The bug this caught: an empty window divided by a clamped divisor produced a confident
    0.0, which became a constant -0.5 buy_share_ma5 and showed up in the screen as coverage on
    data that did not exist."""
    x = np.full(20, np.nan)
    assert np.all(np.isnan(_roll_mean(x, 5)))


def test_roll_mean_handles_a_partially_missing_window():
    x = np.array([np.nan, np.nan, 2.0, 4.0, np.nan, 6.0])
    m = _roll_mean(x, 3)
    assert m[2] == pytest.approx(2.0)      # only one finite value in the window
    assert m[3] == pytest.approx(3.0)      # (2+4)/2
    assert m[5] == pytest.approx(5.0)      # (4+6)/2


def test_flow_features_have_no_coverage_when_the_column_is_missing():
    """Regression: rollup used to drop the flow columns entirely, so every flow feature was
    silently NaN while the screen still printed a row for it."""
    bars = bars_from([100.0 + i for i in range(200)])
    for b in bars:
        b.pop("taker_buy_base")
    rows = {r["feature"]: r for r in screen(bars, horizons=(1,))}
    assert rows["flow:buy_share"]["coverage"] == 0
    assert rows["flow:buy_share_ma5"]["coverage"] == 0
    assert rows["price:ret1"]["coverage"] > 100      # price features unaffected


# --------------------------------------------------------------------------- as-of join
def test_as_of_never_uses_a_future_value():
    """The one join that can leak the future. Funding settles every 8h, so a nearest-neighbour
    match would routinely attach a rate that did not exist yet at bar close — up to eight hours
    of hindsight, which would look like a spectacular edge."""
    from worker.pattern.screen_features import as_of
    series = [(1000, 0.1), (2000, 0.2), (3000, 0.3)]
    got = as_of([999, 1000, 1500, 2000, 2500, 9999], series, 1)
    assert math.isnan(got[0])          # before the series starts — unknown, not 0.1
    assert got[1] == 0.1               # exactly at a publication
    assert got[2] == 0.1               # between: the OLD value, not the next one
    assert got[3] == 0.2
    assert got[4] == 0.2
    assert got[5] == 0.3               # after the end: the last KNOWN value persists


def test_as_of_is_nan_before_the_series_begins():
    """Metrics start ~2021 while klines go back to 2020. Carrying the first known value backwards
    would invent history for a year of bars."""
    from worker.pattern.screen_features import as_of
    got = as_of([0, 1, 2], [(100, 5.0)], 1)
    assert np.all(np.isnan(got))


def test_as_of_handles_an_empty_series():
    from worker.pattern.screen_features import as_of
    assert np.all(np.isnan(as_of([1, 2, 3], [], 1)))


def test_derivative_features_appear_only_when_data_is_supplied():
    bars = bars_from([100.0 + i for i in range(300)])
    assert "fund:rate" not in build_features(bars)
    funding = [(b["timestamp"], 0.0001 * (i % 7 - 3)) for i, b in enumerate(bars)]
    f = build_features(bars, funding=funding)
    assert "fund:rate" in f and "fund:rate_z" in f
    assert np.isfinite(f["fund:rate"]).sum() > 200


def test_planted_funding_signal_is_detected_through_the_as_of_join():
    """End-to-end: if funding really led price, the screen must find it through the join."""
    rng = np.random.default_rng(11)
    n = 3000
    rates = rng.uniform(-0.0003, 0.0003, n)
    closes = [60_000.0]
    for i in range(1, n):
        closes.append(closes[-1] * (1 + rates[i - 1] * 30 + rng.normal(0, 0.0004)))
    bars = bars_from(closes)
    funding = [(b["timestamp"], rates[i]) for i, b in enumerate(bars)]
    rows = {r["feature"]: r for r in screen(bars, horizons=(1,), funding=funding)}
    assert rows["fund:rate"]["ic1"] > 0.3
