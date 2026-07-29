"""Chart patterns — the geometry TA-Lib does not do.

TA-Lib's CDL* functions cover CANDLESTICK patterns (1-3 bars: engulfing, hammer, doji). They do
not cover CHART patterns (double top, head & shoulders, triangles), and no maintained Python
library does either — the handful on PyPI are thin and abandoned. So this is written here,
against the pivot skeleton, which is where the geometry actually lives.

All tolerances are expressed in ATR, never in percent: a 0.5% wobble means something completely
different in a quiet market than a volatile one, and a fixed percentage silently retunes itself
every time volatility changes.

Detection runs over the last `max_pivots` pivots only. These shapes stop meaning anything once
they are far in the past, and bounding the window keeps this O(1) per candle.
"""
from __future__ import annotations

import math

from .pivots import Pivot

DEFAULT_MAX_PIVOTS = 8
# Two pivots are "the same price" within this many ATR — the shoulder/peak matching tolerance.
LEVEL_MATCH_ATR = 0.5
# A head must clear its shoulders by at least this much to be a head and not a third shoulder.
HEAD_PROMINENCE_ATR = 0.75
# Trendline slopes below this (in ATR per bar) count as flat.
FLAT_SLOPE_ATR_PER_BAR = 0.02


def _slope(a: Pivot, b: Pivot, atr: float) -> float:
    """Slope between two pivots in ATR per bar. 0 when they share an index."""
    bars = b.index - a.index
    if bars == 0:
        return 0.0
    return ((b.price - a.price) / atr) / bars


def _same_level(a: float, b: float, atr: float, tolerance_atr: float = LEVEL_MATCH_ATR) -> bool:
    return abs(a - b) <= atr * tolerance_atr


def detect_chart_patterns(
    pivots: list[Pivot], atr: float, *, max_pivots: int = DEFAULT_MAX_PIVOTS
) -> list[dict]:
    """Chart patterns completed by the most recent pivots.

    Returns [{name, direction, ...}] where direction is the bias the pattern implies:
    'bullish', 'bearish', or 'neutral' for shapes that only resolve on the break.
    """
    # 3 is the true floor: a double top is high-low-high. Requiring 4 silently suppressed every
    # double top/bottom whose confirming pivot hadn't printed yet.
    if atr is None or math.isnan(atr) or atr <= 0 or len(pivots) < 3:
        return []

    recent = pivots[-max_pivots:]
    highs = [p for p in recent if p.kind == "high"]
    lows = [p for p in recent if p.kind == "low"]
    found: list[dict] = []

    # --- Double top / bottom: two pivots of the same kind at the same level, with a
    # counter-pivot between them (otherwise it is one wide pivot, not two).
    if len(highs) >= 2 and _same_level(highs[-1].price, highs[-2].price, atr):
        between = [p for p in lows if highs[-2].index < p.index < highs[-1].index]
        if between:
            found.append({
                "name": "double_top",
                "direction": "bearish",
                "level": (highs[-1].price + highs[-2].price) / 2,
                "neckline": min(p.price for p in between),
            })
    if len(lows) >= 2 and _same_level(lows[-1].price, lows[-2].price, atr):
        between = [p for p in highs if lows[-2].index < p.index < lows[-1].index]
        if between:
            found.append({
                "name": "double_bottom",
                "direction": "bullish",
                "level": (lows[-1].price + lows[-2].price) / 2,
                "neckline": max(p.price for p in between),
            })

    # --- Head & shoulders: three highs, middle clearly the tallest, shoulders at a like level.
    if len(highs) >= 3:
        left, head, right = highs[-3], highs[-2], highs[-1]
        shoulders_match = _same_level(left.price, right.price, atr)
        head_clears = (
            head.price - max(left.price, right.price) >= atr * HEAD_PROMINENCE_ATR
        )
        if shoulders_match and head_clears:
            valleys = [p.price for p in lows if left.index < p.index < right.index]
            if valleys:
                found.append({
                    "name": "head_and_shoulders",
                    "direction": "bearish",
                    "level": head.price,
                    "neckline": min(valleys),
                })

    # --- Inverse head & shoulders: the mirror.
    if len(lows) >= 3:
        left, head, right = lows[-3], lows[-2], lows[-1]
        shoulders_match = _same_level(left.price, right.price, atr)
        head_clears = (
            min(left.price, right.price) - head.price >= atr * HEAD_PROMINENCE_ATR
        )
        if shoulders_match and head_clears:
            peaks = [p.price for p in highs if left.index < p.index < right.index]
            if peaks:
                found.append({
                    "name": "inverse_head_and_shoulders",
                    "direction": "bullish",
                    "level": head.price,
                    "neckline": max(peaks),
                })

    # --- Triangles / range: compare the slope of the highs line to the slope of the lows line.
    if len(highs) >= 2 and len(lows) >= 2:
        high_slope = _slope(highs[-2], highs[-1], atr)
        low_slope = _slope(lows[-2], lows[-1], atr)
        high_flat = abs(high_slope) < FLAT_SLOPE_ATR_PER_BAR
        low_flat = abs(low_slope) < FLAT_SLOPE_ATR_PER_BAR

        if high_flat and low_slope > FLAT_SLOPE_ATR_PER_BAR:
            # Flat ceiling, rising floor — buyers absorbing supply at a fixed price.
            found.append({"name": "ascending_triangle", "direction": "bullish",
                          "level": highs[-1].price})
        elif low_flat and high_slope < -FLAT_SLOPE_ATR_PER_BAR:
            found.append({"name": "descending_triangle", "direction": "bearish",
                          "level": lows[-1].price})
        elif high_slope < -FLAT_SLOPE_ATR_PER_BAR and low_slope > FLAT_SLOPE_ATR_PER_BAR:
            # Both sides converging — direction is unknown until one edge breaks.
            found.append({"name": "symmetrical_triangle", "direction": "neutral",
                          "level": (highs[-1].price + lows[-1].price) / 2})
        elif high_flat and low_flat:
            found.append({"name": "range", "direction": "neutral",
                          "level": (highs[-1].price + lows[-1].price) / 2})

    return found
