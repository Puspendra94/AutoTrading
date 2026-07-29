"""Support / resistance levels, clustered from swing pivots.

This is the layer that matters most for the LLM. A pattern NAME ("double top") is not
actionable — it cannot be turned into a stop. A LEVEL is: a price, how many times it has been
respected, and how far away it is in ATR. Those three numbers are what a stop and a target get
built from, so they are what the state pack leads with.

Clustering: pivots within `tolerance_atr` ATR of each other are one level, priced at the mean
of its members. Touch count is the cluster size — a level tapped four times is a stronger
statement than one tapped twice.
"""
from __future__ import annotations

import math

from .arrays import Candles
from .pivots import Pivot

DEFAULT_TOLERANCE_ATR = 0.35
MAX_LEVELS_PER_SIDE = 4


def _cluster(prices: list[float], tolerance: float) -> list[tuple[float, int]]:
    """Greedy 1-D clustering over sorted prices. Returns [(mean_price, touch_count)]."""
    if not prices:
        return []
    ordered = sorted(prices)
    clusters: list[list[float]] = [[ordered[0]]]
    for price in ordered[1:]:
        # Compare against the cluster MEAN, not its first member, so a run of slowly drifting
        # pivots doesn't chain into one absurdly wide "level".
        current = clusters[-1]
        if abs(price - (sum(current) / len(current))) <= tolerance:
            current.append(price)
        else:
            clusters.append([price])
    return [(sum(group) / len(group), len(group)) for group in clusters]


def build_levels(
    c: Candles,
    pivots: list[Pivot],
    atr: float,
    *,
    tolerance_atr: float = DEFAULT_TOLERANCE_ATR,
    max_per_side: int = MAX_LEVELS_PER_SIDE,
) -> dict:
    """Support below / resistance above the current price, nearest first.

    A level is classified by where it sits relative to price NOW, not by whether it came from a
    swing high or low: broken resistance becomes support, and the pivot's original type is
    irrelevant once price is on the other side of it.
    """
    close = float(c.close[-1])
    if not pivots or atr is None or math.isnan(atr) or atr <= 0:
        return {"resistance": [], "support": [], "rangeHigh": None, "rangeLow": None}

    tolerance = atr * tolerance_atr
    clusters = _cluster([p.price for p in pivots], tolerance)

    resistance = [
        {"price": price, "touches": touches, "distAtr": (price - close) / atr}
        for price, touches in clusters
        if price > close
    ]
    support = [
        {"price": price, "touches": touches, "distAtr": (close - price) / atr}
        for price, touches in clusters
        if price < close
    ]

    resistance.sort(key=lambda lv: lv["distAtr"])
    support.sort(key=lambda lv: lv["distAtr"])

    # The range is the outermost confirmed structure, which is what a breakout breaks out of.
    highs = [p.price for p in pivots if p.kind == "high"]
    lows = [p.price for p in pivots if p.kind == "low"]

    return {
        "resistance": resistance[:max_per_side],
        "support": support[:max_per_side],
        "rangeHigh": max(highs) if highs else None,
        "rangeLow": min(lows) if lows else None,
    }
