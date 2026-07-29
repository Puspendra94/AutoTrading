"""Swing pivots — the skeleton every chart pattern and S/R level is built from.

Fractal rule: a bar is a swing HIGH when its high is the strict maximum of the `width` bars on
each side (mirrored for a swing LOW). Pure numpy, so this adds no dependency — scipy's
find_peaks would do the same job but pulls ~30MB in for twenty lines of comparison.

The `width` bars on the right are what make a pivot reliable: a pivot is only confirmed once
`width` further bars have printed without exceeding it. That means the most recent pivot is
always at least `width` bars old — which is correct, not a bug. A "pivot" on the live bar would
repaint on the next tick, and a level that repaints is worse than no level.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .arrays import Candles

DEFAULT_WIDTH = 3


@dataclass(frozen=True)
class Pivot:
    index: int  # position in the candle array
    price: float
    kind: str  # 'high' | 'low'
    time_ms: float


def find_pivots(c: Candles, width: int = DEFAULT_WIDTH) -> list[Pivot]:
    """All confirmed swing pivots, oldest first, highs and lows interleaved in time order."""
    if width < 1:
        raise ValueError("pivot width must be >= 1")
    n = len(c)
    pivots: list[Pivot] = []
    if n < 2 * width + 1:
        return pivots

    for i in range(width, n - width):
        left = slice(i - width, i)
        right = slice(i + 1, i + 1 + width)

        # Strict on both sides: a flat shelf of equal highs is a range boundary, not a pivot,
        # and admitting it would emit a cluster of duplicate levels at the same price.
        if c.high[i] > c.high[left].max() and c.high[i] > c.high[right].max():
            pivots.append(Pivot(i, float(c.high[i]), "high", float(c.time_ms[i])))
        elif c.low[i] < c.low[left].min() and c.low[i] < c.low[right].min():
            pivots.append(Pivot(i, float(c.low[i]), "low", float(c.time_ms[i])))

    return pivots


def last_pivot(pivots: list[Pivot], kind: str) -> Pivot | None:
    for p in reversed(pivots):
        if p.kind == kind:
            return p
    return None


def structure(pivots: list[Pivot]) -> dict:
    """Market structure from the last two highs and last two lows.

    higherHighs + higherLows is an uptrend's definition; both false with lowerHighs/lowerLows is
    a downtrend; mixed is chop. Reported as raw booleans so the regime layer decides what to
    call it and the LLM can see the evidence rather than only the verdict.
    """
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]

    higher_highs = len(highs) >= 2 and highs[-1].price > highs[-2].price
    higher_lows = len(lows) >= 2 and lows[-1].price > lows[-2].price
    lower_highs = len(highs) >= 2 and highs[-1].price < highs[-2].price
    lower_lows = len(lows) >= 2 and lows[-1].price < lows[-2].price

    return {
        "lastSwingHigh": highs[-1].price if highs else None,
        "lastSwingLow": lows[-1].price if lows else None,
        "higherHighs": higher_highs,
        "higherLows": higher_lows,
        "lowerHighs": lower_highs,
        "lowerLows": lower_lows,
        "pivotCount": len(pivots),
    }
