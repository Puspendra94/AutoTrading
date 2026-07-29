"""Indicator layer — TA-Lib called directly on numpy arrays.

Direct rather than through pandas-ta on purpose: pandas-ta's `cdl_pattern` only delegates to
TA-Lib anyway, its last release (0.3.14b, 2021) does `from numpy import NaN`, which is gone in
numpy 2.x — and this project is on numpy 2.5. So it would buy dataframe ergonomics in exchange
for a pinned-back numpy.

TA-Lib emits NaN for the warm-up bars of every indicator (and ADX needs ~2x its period), so the
`last_*` values here are NaN until enough history exists. Callers must treat NaN as "unknown"
rather than a number — `has_warmup` is the cheap check.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import talib

from .arrays import Candles

EMA_FAST_PERIOD = 20
EMA_MID_PERIOD = 50
EMA_SLOW_PERIOD = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
ADX_PERIOD = 14
VOLUME_MA_PERIOD = 20

# Bars needed before every indicator above is defined. EMA200 is the long pole; the margin on
# top gives ADX (which warms up over roughly 2x its period) somewhere to settle.
MIN_BARS = EMA_SLOW_PERIOD + 60

# The candlestick patterns worth surfacing. TA-Lib ships 61; feeding all of them to an LLM is
# noise, and most fire constantly on crypto. These are the reversal/continuation signals that
# actually carry information at a swing point. Values are -100 (bearish) / 0 / +100 (bullish);
# some TA-Lib functions also emit +/-200 for a stronger variant, so callers compare by sign.
CANDLE_PATTERNS: dict[str, str] = {
    "engulfing": "CDLENGULFING",
    "hammer": "CDLHAMMER",
    "inverted_hammer": "CDLINVERTEDHAMMER",
    "shooting_star": "CDLSHOOTINGSTAR",
    "hanging_man": "CDLHANGINGMAN",
    "morning_star": "CDLMORNINGSTAR",
    "evening_star": "CDLEVENINGSTAR",
    "doji": "CDLDOJI",
    "dragonfly_doji": "CDLDRAGONFLYDOJI",
    "gravestone_doji": "CDLGRAVESTONEDOJI",
    "three_white_soldiers": "CDL3WHITESOLDIERS",
    "three_black_crows": "CDL3BLACKCROWS",
    "piercing": "CDLPIERCING",
    "dark_cloud_cover": "CDLDARKCLOUDCOVER",
    "harami": "CDLHARAMI",
    "marubozu": "CDLMARUBOZU",
}


def _last(series: np.ndarray) -> float:
    """Final value of a TA-Lib output, as a plain float (NaN while warming up)."""
    if series.size == 0:
        return float("nan")
    return float(series[-1])


@dataclass(frozen=True)
class Indicators:
    ema_fast: np.ndarray
    ema_mid: np.ndarray
    ema_slow: np.ndarray
    rsi: np.ndarray
    atr: np.ndarray
    adx: np.ndarray
    volume_ma: np.ndarray

    @property
    def has_warmup(self) -> bool:
        """True when every indicator is defined on the final bar."""
        return not any(
            math.isnan(v)
            for v in (
                _last(self.ema_fast), _last(self.ema_mid), _last(self.ema_slow),
                _last(self.rsi), _last(self.atr), _last(self.adx),
            )
        )


def compute_indicators(c: Candles) -> Indicators:
    return Indicators(
        ema_fast=talib.EMA(c.close, timeperiod=EMA_FAST_PERIOD),
        ema_mid=talib.EMA(c.close, timeperiod=EMA_MID_PERIOD),
        ema_slow=talib.EMA(c.close, timeperiod=EMA_SLOW_PERIOD),
        rsi=talib.RSI(c.close, timeperiod=RSI_PERIOD),
        atr=talib.ATR(c.high, c.low, c.close, timeperiod=ATR_PERIOD),
        adx=talib.ADX(c.high, c.low, c.close, timeperiod=ADX_PERIOD),
        volume_ma=talib.SMA(c.volume, timeperiod=VOLUME_MA_PERIOD),
    )


def summarize(c: Candles, ind: Indicators) -> dict:
    """Final-bar indicator values, as the state pack presents them.

    atr_pct is ATR as a percentage of price — the unit that actually travels between symbols and
    the one the stop-distance rules are written in.
    """
    atr = _last(ind.atr)
    close = float(c.close[-1])
    atr_pct = (atr / close * 100.0) if close and not math.isnan(atr) else float("nan")
    return {
        "ema20": _last(ind.ema_fast),
        "ema50": _last(ind.ema_mid),
        "ema200": _last(ind.ema_slow),
        "rsi14": _last(ind.rsi),
        "atr14": atr,
        "atrPct": atr_pct,
        "adx14": _last(ind.adx),
        "volume": float(c.volume[-1]),
        "volumeMa20": _last(ind.volume_ma),
    }


def detect_candle_patterns(c: Candles, lookback: int = 1) -> list[dict]:
    """Candlestick patterns that fired within the last `lookback` bars.

    Returns [{name, direction, barsAgo}] — `direction` from the sign of TA-Lib's output, so the
    +/-200 "strong" variants classify the same as +/-100.
    """
    if len(c) < 5:  # the longest pattern here spans 3 bars and needs prior context
        return []

    hits: list[dict] = []
    for name, fn_name in CANDLE_PATTERNS.items():
        out = getattr(talib, fn_name)(c.open, c.high, c.low, c.close)
        window = out[-lookback:]
        nonzero = np.nonzero(window)[0]
        if nonzero.size == 0:
            continue
        idx = int(nonzero[-1])  # most recent occurrence inside the window
        value = float(window[idx])
        hits.append({
            "name": name,
            "direction": "bullish" if value > 0 else "bearish",
            "barsAgo": int(lookback - 1 - idx),
        })
    return hits
