"""Feature-engine tests (Phase 1 of the pattern-brain pivot).

Synthetic candles throughout so every assertion is exact and reproducible — no DB, no network,
no TA-Lib warm-up ambiguity hidden behind real market noise.
"""
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np

from worker.pattern.features.arrays import to_arrays
from worker.pattern.features.indicators import MIN_BARS, compute_indicators
from worker.pattern.features.levels import build_levels
from worker.pattern.features.patterns import detect_chart_patterns
from worker.pattern.features.pivots import find_pivots, structure
from worker.pattern.features.regime import classify_regime, regime_bias
from worker.pattern.features.state import build_feature_state
from worker.pattern.features.triggers import detect_triggers

MINUTE_MS = 60_000


def candle(i, o, h, l, c, v=100.0):
    return {"timestamp": i * MINUTE_MS, "open": o, "high": h, "low": l, "close": c, "volume": v}


def flat_series(closes, volume=100.0):
    """Candles whose high/low hug the close — isolates whatever the test is actually varying."""
    return [
        candle(i, c, c + 1, c - 1, c, volume) for i, c in enumerate(closes)
    ]


def zigzag(peaks_and_troughs, span=5):
    """Interpolate between turning points to build a clean swing series."""
    closes = []
    for a, b in zip(peaks_and_troughs, peaks_and_troughs[1:]):
        closes.extend(np.linspace(a, b, span, endpoint=False).tolist())
    closes.append(peaks_and_troughs[-1])
    return closes


# --------------------------------------------------------------------------- arrays
def test_to_arrays_accepts_decimals_and_datetimes():
    """asyncpg hands back Decimal + datetime; TA-Lib needs contiguous float64 or it raises."""
    rows = [{
        "timestamp": datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
        "open": Decimal("100.5"), "high": Decimal("101"), "low": Decimal("99"),
        "close": Decimal("100.75"), "volume": Decimal("12.5"),
    }]
    c = to_arrays(rows)
    assert c.close.dtype == np.float64
    assert c.close[0] == 100.75
    assert c.time_ms[0] == datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc).timestamp() * 1000


# --------------------------------------------------------------------------- pivots
def test_pivots_found_at_turning_points():
    closes = zigzag([100, 120, 100, 130, 100], span=6)
    pivots = find_pivots(to_arrays(flat_series(closes)), width=3)

    assert [p.kind for p in pivots] == ["high", "low", "high"]
    assert pivots[0].price > 115  # the 120 peak
    assert pivots[-1].price > 125  # the 130 peak


def test_recent_bars_cannot_be_pivots():
    """A pivot needs `width` bars of confirmation on its right, so the newest bars are never
    pivots. This is what stops levels repainting as new candles arrive."""
    c = to_arrays(flat_series(zigzag([100, 140, 100], span=8)))
    pivots = find_pivots(c, width=3)

    assert pivots, "expected at least one confirmed pivot"
    assert max(p.index for p in pivots) <= len(c) - 1 - 3


def test_structure_reports_lower_highs_in_a_downtrend():
    closes = zigzag([150, 120, 140, 110, 130, 100], span=6)
    s = structure(find_pivots(to_arrays(flat_series(closes)), width=3))

    assert s["lowerHighs"] is True
    assert s["lowerLows"] is True
    assert s["higherHighs"] is False


# --------------------------------------------------------------------------- levels
def test_levels_cluster_by_touch_count_and_split_around_price():
    c = to_arrays(flat_series([100] * 10 + [110]))  # price now 110
    atr = 2.0

    class P:  # minimal pivot stand-ins at three prices, one repeated three times
        def __init__(self, price, kind):
            self.price, self.kind, self.index, self.time_ms = price, kind, 0, 0.0

    pivots = [P(120.0, "high"), P(120.3, "high"), P(119.8, "high"), P(105.0, "low")]
    levels = build_levels(c, pivots, atr)

    assert len(levels["resistance"]) == 1
    assert levels["resistance"][0]["touches"] == 3  # the three ~120 pivots merged
    assert abs(levels["resistance"][0]["price"] - 120.03) < 0.1
    assert levels["support"][0]["price"] == 105.0
    # 120.03 is ~10 above a price of 110, at ATR 2 -> ~5 ATR away.
    assert abs(levels["resistance"][0]["distAtr"] - 5.0) < 0.1


def test_levels_empty_without_atr():
    """A NaN ATR means the warm-up hasn't finished; emitting levels anyway would produce
    distances measured in nothing."""
    c = to_arrays(flat_series([100] * 5))
    assert build_levels(c, [], float("nan")) == {
        "resistance": [], "support": [], "rangeHigh": None, "rangeLow": None,
    }


# --------------------------------------------------------------------------- chart patterns
def test_double_top_detected():
    closes = zigzag([100, 130, 110, 130.2, 105], span=6)
    pivots = find_pivots(to_arrays(flat_series(closes)), width=3)
    names = [p["name"] for p in detect_chart_patterns(pivots, atr=2.0)]

    assert "double_top" in names


def test_head_and_shoulders_detected():
    closes = zigzag([100, 125, 110, 145, 110, 125.3, 100], span=6)
    pivots = find_pivots(to_arrays(flat_series(closes)), width=3)
    found = {p["name"]: p for p in detect_chart_patterns(pivots, atr=2.0)}

    assert "head_and_shoulders" in found
    assert found["head_and_shoulders"]["direction"] == "bearish"
    assert found["head_and_shoulders"]["neckline"] < found["head_and_shoulders"]["level"]


def test_no_patterns_without_atr():
    assert detect_chart_patterns([], atr=float("nan")) == []


# --------------------------------------------------------------------------- regime
def test_regime_uptrend_and_bias():
    c = to_arrays(flat_series([100 + i * 0.9 for i in range(MIN_BARS)]))
    r = classify_regime(c, compute_indicators(c))

    assert r["label"] in ("uptrend", "strong_uptrend")
    assert r["emaStack"] == "20>50>200"
    assert regime_bias(r["label"]) == "long"


def test_regime_downtrend():
    c = to_arrays(flat_series([400 - i * 0.9 for i in range(MIN_BARS)]))
    r = classify_regime(c, compute_indicators(c))

    assert r["label"] in ("downtrend", "strong_downtrend")
    assert regime_bias(r["label"]) == "short"


def test_choppy_market_is_range_not_a_trend():
    closes = [100 + (5 if i % 2 else -5) for i in range(MIN_BARS)]
    c = to_arrays(flat_series(closes))
    r = classify_regime(c, compute_indicators(c))

    assert r["label"] == "range"
    assert regime_bias(r["label"]) == "none"


def test_regime_falls_back_to_range_while_warming_up():
    """Too little history must never produce a directional label."""
    c = to_arrays(flat_series([100 + i for i in range(30)]))
    assert classify_regime(c, compute_indicators(c))["label"] == "range"


# --------------------------------------------------------------------------- triggers
def _levels_with_resistance(price, touches=3):
    return {"resistance": [{"price": price, "touches": touches, "distAtr": 0.5}],
            "support": [], "rangeHigh": None, "rangeLow": None}


def test_resistance_break_fires_on_the_crossing_bar_only():
    rising = [100 + i * 0.9 for i in range(MIN_BARS)]
    c = to_arrays(flat_series(rising))
    ind = compute_indicators(c)
    regime = classify_regime(c, ind)
    level = float(c.close[-2]) + 0.1  # crossed by the final bar

    crossed = detect_triggers(c, ind, _levels_with_resistance(level), [], regime)
    assert [t["name"] for t in crossed] == ["resistance_break"]
    assert crossed[0]["side"] == "long"

    # Same level, but already far below both closes -> the break is old news, not a trigger.
    stale = detect_triggers(c, ind, _levels_with_resistance(float(c.close[-2]) - 50), [], regime)
    assert [t["name"] for t in stale] == []


def test_neutral_chart_pattern_produces_no_trigger():
    c = to_arrays(flat_series([100 + i * 0.9 for i in range(MIN_BARS)]))
    ind = compute_indicators(c)
    regime = classify_regime(c, ind)
    patterns = [{"name": "symmetrical_triangle", "direction": "neutral", "level": 100.0}]

    triggers = detect_triggers(c, ind, {"resistance": [], "support": []}, patterns, regime)
    assert [t["name"] for t in triggers] == []


# --------------------------------------------------------------------------- state
def test_state_is_cold_and_triggerless_on_short_history():
    candles = flat_series([100 + i for i in range(50)])
    st = build_feature_state("t1", "BTCUSDT", "15m", candles)

    assert st.warm is False
    assert st.triggers == []
    assert st.bias == "none"


def test_state_pack_is_strict_json_serialisable():
    """NaN is not valid JSON — a warming-up indicator must serialise as null, not `NaN`."""
    import json

    st = build_feature_state("t1", "BTCUSDT", "15m", flat_series([100 + i for i in range(50)]))
    pack = st.to_state_pack()

    text = json.dumps(pack, allow_nan=False)  # raises if any NaN survived
    assert "NaN" not in text
    assert pack["indicators"]["ema200"] is None  # not warm yet
    assert pack["market"]["symbol"] == "BTCUSDT"


def test_state_pack_is_warm_and_populated_on_full_history():
    st = build_feature_state(
        "t1", "BTCUSDT", "15m", flat_series([100 + i * 0.9 for i in range(MIN_BARS + 50)])
    )
    pack = st.to_state_pack()

    assert st.warm is True
    assert pack["regime"]["label"] in ("uptrend", "strong_uptrend")
    assert pack["indicators"]["atr14"] > 0
    assert pack["indicators"]["ema200"] is not None
