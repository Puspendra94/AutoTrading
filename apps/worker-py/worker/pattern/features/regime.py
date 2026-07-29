"""Regime classification — one label for "what kind of market is this".

This is the cheapest and most valuable filter in the system. Deciding regime deterministically,
before any LLM call, is what lets the gate skip most candles for free: there is no point paying
for a decision in the middle of a directionless chop.

Inputs are deliberately boring and standard — EMA stack, ADX, and the slope of the fast EMA in
ATR per bar — because the regime label must be reproducible and explainable when a trade is
reviewed later.
"""
from __future__ import annotations

import math

import numpy as np

from .arrays import Candles
from .indicators import Indicators

# ADX convention: <20 no trend, 20-25 emerging, >25 trending, >40 strong.
ADX_TRENDING = 25.0
ADX_STRONG = 40.0
# Fast-EMA slope over SLOPE_LOOKBACK bars, in ATR per bar, before a trend counts as "moving".
SLOPE_LOOKBACK = 5
SLOPE_MIN_ATR_PER_BAR = 0.05

STRONG_UPTREND = "strong_uptrend"
UPTREND = "uptrend"
RANGE = "range"
DOWNTREND = "downtrend"
STRONG_DOWNTREND = "strong_downtrend"

BULLISH_REGIMES = (UPTREND, STRONG_UPTREND)
BEARISH_REGIMES = (DOWNTREND, STRONG_DOWNTREND)


def _ema_stack(fast: float, mid: float, slow: float) -> str:
    if math.isnan(fast) or math.isnan(mid) or math.isnan(slow):
        return "unknown"
    if fast > mid > slow:
        return "20>50>200"
    if fast < mid < slow:
        return "20<50<200"
    return "mixed"


def _at(series: np.ndarray, at: int) -> float:
    """Value at a (possibly negative) index, or NaN when the series is too short."""
    if series.size == 0 or abs(at) > series.size:
        return float("nan")
    return float(series[at])


def _slope_atr_per_bar(
    series: np.ndarray, atr: float, at: int = -1, lookback: int = SLOPE_LOOKBACK
) -> float:
    start_at = at - lookback
    if math.isnan(atr) or atr <= 0 or abs(start_at) > series.size:
        return float("nan")
    start, end = _at(series, start_at), _at(series, at)
    if math.isnan(start) or math.isnan(end):
        return float("nan")
    return ((end - start) / atr) / lookback


def classify_regime(c: Candles, ind: Indicators, at: int = -1) -> dict:
    """Return {label, adx, emaStack, slopeAtr} for the bar at `at` (default: the last one).

    `at` is parameterised so the trigger layer can classify the PREVIOUS bar too and spot a
    regime flip, without paying to recompute every indicator over a truncated array.

    Falls back to 'range' — never to a directional label — whenever the inputs are still warming
    up. An unknown market must not read as a tradeable one.
    """
    atr = _at(ind.atr, at)
    adx = _at(ind.adx, at)
    stack = _ema_stack(_at(ind.ema_fast, at), _at(ind.ema_mid, at), _at(ind.ema_slow, at))
    slope = _slope_atr_per_bar(ind.ema_fast, atr, at)

    label = RANGE
    if not math.isnan(adx) and not math.isnan(slope) and adx >= ADX_TRENDING:
        # ADX measures trend STRENGTH but is direction-blind, so direction comes from the EMA
        # stack and the slope agreeing. Requiring both is what keeps a violent counter-trend
        # spike inside a downtrend from being labelled an uptrend.
        if stack == "20>50>200" and slope >= SLOPE_MIN_ATR_PER_BAR:
            label = STRONG_UPTREND if adx >= ADX_STRONG else UPTREND
        elif stack == "20<50<200" and slope <= -SLOPE_MIN_ATR_PER_BAR:
            label = STRONG_DOWNTREND if adx >= ADX_STRONG else DOWNTREND

    return {"label": label, "adx": adx, "emaStack": stack, "slopeAtr": slope}


def regime_bias(label: str) -> str:
    """'long' | 'short' | 'none' — the side this regime permits."""
    if label in BULLISH_REGIMES:
        return "long"
    if label in BEARISH_REGIMES:
        return "short"
    return "none"
