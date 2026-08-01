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

# --- Strength scoring -------------------------------------------------------------------------
# Shapes are matched independently against different pivot subsets — a double top reads the highs,
# an inverse head & shoulders reads the lows — so in a choppy range a bullish and a bearish pattern
# genuinely complete on the same bar. Observed live: three `pattern_complete` triggers on one bar,
# a short at 63123 and a long at 63127. Nothing downstream could rank them, so every such bar was
# skipped as "contradictory" and the engine never traded.
#
# Detection is deliberately left alone — suppressing a real shape would be lying about the chart.
# Instead each pattern carries a comparable score so the decision layer can prefer one.
#
# A pattern this tall (in ATR) scores full marks for prominence; taller adds nothing.
#
# 3.0, not 2.0: at 2.0 the real conflicting bar saturated BOTH sides at 1.00 and the score stopped
# discriminating exactly when it was needed. Saturation has to sit above the size of a typical
# competing pair or the tie-break is decorative.
PROMINENCE_FULL_ATR = 3.0
# Recency decays to zero this many bars after the pattern's last defining pivot.
RECENCY_FADE_BARS = 20.0
# Size matters more than freshness: a big clean shape beats a marginally more recent scrappy one.
PROMINENCE_WEIGHT = 0.6
RECENCY_WEIGHT = 0.4

# Shapes are not equally strong evidence, and their "prominence" is not measured the same way.
# A completed reversal has already printed the turn; a triangle is a coiling continuation shape
# that has NOT resolved yet, and its span is the whole structure rather than a committed move.
# Without this, a triangle outranks a completed reversal purely because the structure is tall.
PATTERN_RELIABILITY = {
    "double_top": 1.0,
    "double_bottom": 1.0,
    "head_and_shoulders": 1.0,
    "inverse_head_and_shoulders": 1.0,
    "ascending_triangle": 0.7,
    "descending_triangle": 0.7,
    "symmetrical_triangle": 0.7,
    "range": 0.7,
}


def _scored(entry: dict, *, prominence_atr: float, last_index: int, newest_index: int) -> dict:
    """Attach `prominenceAtr`, `barsSinceCompletion` and a blended 0-1 `strength`.

    Both inputs are already ATR-relative or in bars, so the score means the same thing in a quiet
    market as a volatile one.
    """
    bars_since = max(0, newest_index - last_index)
    prominence = max(0.0, prominence_atr)
    prom_component = min(prominence / PROMINENCE_FULL_ATR, 1.0)
    recency_component = max(0.0, 1.0 - bars_since / RECENCY_FADE_BARS)
    reliability = PATTERN_RELIABILITY.get(entry["name"], 1.0)
    entry["prominenceAtr"] = round(prominence, 2)
    entry["barsSinceCompletion"] = bars_since
    entry["strength"] = round(
        (PROMINENCE_WEIGHT * prom_component + RECENCY_WEIGHT * recency_component) * reliability, 2
    )
    return entry


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
    newest_index = recent[-1].index
    found: list[dict] = []

    # --- Double top / bottom: two pivots of the same kind at the same level, with a
    # counter-pivot between them (otherwise it is one wide pivot, not two).
    if len(highs) >= 2 and _same_level(highs[-1].price, highs[-2].price, atr):
        between = [p for p in lows if highs[-2].index < p.index < highs[-1].index]
        if between:
            level = (highs[-1].price + highs[-2].price) / 2
            neckline = min(p.price for p in between)
            found.append(_scored(
                {"name": "double_top", "direction": "bearish",
                 "level": level, "neckline": neckline},
                # Height of the top above the neckline: how far it can fall if it resolves.
                prominence_atr=(level - neckline) / atr,
                last_index=highs[-1].index, newest_index=newest_index,
            ))
    if len(lows) >= 2 and _same_level(lows[-1].price, lows[-2].price, atr):
        between = [p for p in highs if lows[-2].index < p.index < lows[-1].index]
        if between:
            level = (lows[-1].price + lows[-2].price) / 2
            neckline = max(p.price for p in between)
            found.append(_scored(
                {"name": "double_bottom", "direction": "bullish",
                 "level": level, "neckline": neckline},
                prominence_atr=(neckline - level) / atr,
                last_index=lows[-1].index, newest_index=newest_index,
            ))

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
                found.append(_scored(
                    {"name": "head_and_shoulders", "direction": "bearish",
                     "level": head.price, "neckline": min(valleys)},
                    # How far the head clears its shoulders is what makes this a head at all.
                    prominence_atr=(head.price - max(left.price, right.price)) / atr,
                    last_index=right.index, newest_index=newest_index,
                ))

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
                found.append(_scored(
                    {"name": "inverse_head_and_shoulders", "direction": "bullish",
                     "level": head.price, "neckline": max(peaks)},
                    prominence_atr=(min(left.price, right.price) - head.price) / atr,
                    last_index=right.index, newest_index=newest_index,
                ))

    # --- Triangles / range: compare the slope of the highs line to the slope of the lows line.
    if len(highs) >= 2 and len(lows) >= 2:
        high_slope = _slope(highs[-2], highs[-1], atr)
        low_slope = _slope(lows[-2], lows[-1], atr)
        high_flat = abs(high_slope) < FLAT_SLOPE_ATR_PER_BAR
        low_flat = abs(low_slope) < FLAT_SLOPE_ATR_PER_BAR

        # Height of the structure the price is coiling inside — the move available on a break.
        span_atr = (highs[-1].price - lows[-1].price) / atr
        edge_index = max(highs[-1].index, lows[-1].index)

        if high_flat and low_slope > FLAT_SLOPE_ATR_PER_BAR:
            # Flat ceiling, rising floor — buyers absorbing supply at a fixed price.
            found.append(_scored(
                {"name": "ascending_triangle", "direction": "bullish", "level": highs[-1].price},
                prominence_atr=span_atr, last_index=edge_index, newest_index=newest_index,
            ))
        elif low_flat and high_slope < -FLAT_SLOPE_ATR_PER_BAR:
            found.append(_scored(
                {"name": "descending_triangle", "direction": "bearish", "level": lows[-1].price},
                prominence_atr=span_atr, last_index=edge_index, newest_index=newest_index,
            ))
        elif high_slope < -FLAT_SLOPE_ATR_PER_BAR and low_slope > FLAT_SLOPE_ATR_PER_BAR:
            # Both sides converging — direction is unknown until one edge breaks.
            found.append(_scored(
                {"name": "symmetrical_triangle", "direction": "neutral",
                 "level": (highs[-1].price + lows[-1].price) / 2},
                prominence_atr=span_atr, last_index=edge_index, newest_index=newest_index,
            ))
        elif high_flat and low_flat:
            found.append(_scored(
                {"name": "range", "direction": "neutral",
                 "level": (highs[-1].price + lows[-1].price) / 2},
                prominence_atr=span_atr, last_index=edge_index, newest_index=newest_index,
            ))

    return found
