"""Deterministic technical-indicator library for the strategy DSL.

Originally a parity port of a TypeScript twin, but the backend engine was deleted in the
single-engine consolidation (Phase B) — this is now the ONE implementation, so new indicators
(e.g. adx) live here only.

Series is a dict of equal-length float lists: {"open","high","low","close","volume"}.
Positions before an indicator is defined hold float('nan'); the interpreter treats any condition
touching a NaN as false.
"""
from __future__ import annotations

import math

import numpy as np

try:  # TA-Lib needs the native ta-lib C library; guarded so the module imports even if absent.
    import talib as _talib
except Exception:  # noqa: BLE001
    _talib = None

NAN = float("nan")

# Curated candlestick reversal patterns exposed to the DSL (friendly name -> TA-Lib CDL function).
# Value per bar: +100 bullish, -100 bearish, 0 none. Chosen for reliability + coverage of the
# reversals a strategy actually cares about — not all 61 TA-Lib patterns (that would just be noise
# for the LLM to overfit to).
CANDLESTICK_PATTERNS: dict[str, str] = {
    "engulfing": "CDLENGULFING",
    "hammer": "CDLHAMMER",
    "invertedHammer": "CDLINVERTEDHAMMER",
    "shootingStar": "CDLSHOOTINGSTAR",
    "hangingMan": "CDLHANGINGMAN",
    "doji": "CDLDOJI",
    "morningStar": "CDLMORNINGSTAR",
    "eveningStar": "CDLEVENINGSTAR",
    "harami": "CDLHARAMI",
    "piercing": "CDLPIERCING",
    "darkCloudCover": "CDLDARKCLOUDCOVER",
    "threeWhiteSoldiers": "CDL3WHITESOLDIERS",
    "threeBlackCrows": "CDL3BLACKCROWS",
}


def _nan(n: int) -> list[float]:
    return [NAN] * n


def candlestick(s: dict, pattern: str) -> list[float]:
    """Candlestick-pattern signal per bar via TA-Lib: +100 (bullish), -100 (bearish), 0 (none).
    Unknown pattern or missing TA-Lib -> all-NaN (the interpreter treats NaN conditions as false)."""
    fn_name = CANDLESTICK_PATTERNS.get(pattern)
    if fn_name is None or _talib is None:
        return _nan(len(s["close"]))
    fn = getattr(_talib, fn_name)
    arr = fn(
        np.asarray(s["open"], dtype="float64"),
        np.asarray(s["high"], dtype="float64"),
        np.asarray(s["low"], dtype="float64"),
        np.asarray(s["close"], dtype="float64"),
    )
    return [float(x) for x in arr]


def ema(prices: list[float], period: int) -> list[float]:
    out = [0.0] * len(prices)
    if not prices:
        return out
    k = 2 / (period + 1)
    out[0] = prices[0]
    for i in range(1, len(prices)):
        out[i] = prices[i] * k + out[i - 1] * (1 - k)
    return out


# NaN-safe window mean: returns NaN until there are `period` consecutive finite values ending
# at i. A sliding-sum would be poisoned forever by the NaN warmup-prefix of a source indicator
# (e.g. Stochastic smoothing the raw-%K series), so each window is summed directly.
def sma(prices: list[float], period: int) -> list[float]:
    out = _nan(len(prices))
    for i in range(period - 1, len(prices)):
        total = 0.0
        ok = True
        for j in range(i - period + 1, i + 1):
            if not math.isfinite(prices[j]):
                ok = False
                break
            total += prices[j]
        if ok:
            out[i] = total / period
    return out


def _rolling_std(prices: list[float], period: int, means: list[float]) -> list[float]:
    out = _nan(len(prices))
    for i in range(period - 1, len(prices)):
        mean = means[i]
        acc = 0.0
        for j in range(i - period + 1, i + 1):
            acc += (prices[j] - mean) ** 2
        out[i] = math.sqrt(acc / period)
    return out


def rsi(prices: list[float], period: int) -> list[float]:
    out = _nan(len(prices))
    if len(prices) <= period:
        return out
    avg_gain = 0.0
    avg_loss = 0.0
    for i in range(1, period + 1):
        change = prices[i] - prices[i - 1]
        if change >= 0:
            avg_gain += change
        else:
            avg_loss -= change
    avg_gain /= period
    avg_loss /= period
    out[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(prices)):
        change = prices[i] - prices[i - 1]
        gain = change if change > 0 else 0
        loss = -change if change < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def atr(s: dict, period: int) -> list[float]:
    high, low, close = s["high"], s["low"], s["close"]
    n = len(close)
    out = _nan(n)
    if n <= period:
        return out
    tr = [0.0] * n
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    total = 0.0
    for i in range(1, period + 1):
        total += tr[i]
    out[period] = total / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def adx(s: dict, period: int) -> list[float]:
    """Average Directional Index (Wilder) — trend-strength (0-100; >25 = trending). Added in the
    single-engine (Phase C) era, so worker-only (no TS parity). Uses Wilder smoothing throughout,
    matching the atr() convention above."""
    high, low, close = s["high"], s["low"], s["close"]
    n = len(close)
    out = _nan(n)
    if n <= 2 * period:  # need `period` bars to seed the DIs, then `period` more to seed the ADX
        return out

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    # Wilder-smoothed TR / +DM / -DM (seed = sum over the first `period` bars starting at index 1).
    atr_s = [0.0] * n
    pdm_s = [0.0] * n
    mdm_s = [0.0] * n
    atr_s[period] = sum(tr[1:period + 1])
    pdm_s[period] = sum(plus_dm[1:period + 1])
    mdm_s[period] = sum(minus_dm[1:period + 1])
    for i in range(period + 1, n):
        atr_s[i] = atr_s[i - 1] - atr_s[i - 1] / period + tr[i]
        pdm_s[i] = pdm_s[i - 1] - pdm_s[i - 1] / period + plus_dm[i]
        mdm_s[i] = mdm_s[i - 1] - mdm_s[i - 1] / period + minus_dm[i]

    dx = [0.0] * n
    for i in range(period, n):
        if atr_s[i] == 0:
            continue
        plus_di = 100 * pdm_s[i] / atr_s[i]
        minus_di = 100 * mdm_s[i] / atr_s[i]
        denom = plus_di + minus_di
        dx[i] = 100 * abs(plus_di - minus_di) / denom if denom != 0 else 0.0

    # ADX = Wilder-smoothed DX; first value at index 2*period-1 = mean of DX[period .. 2*period-1].
    out[2 * period - 1] = sum(dx[period:2 * period]) / period
    for i in range(2 * period, n):
        out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out


def macd(prices: list[float], fast: int, slow: int, signal_period: int) -> dict:
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    line = [ema_fast[i] - ema_slow[i] for i in range(len(prices))]
    signal = ema(line, signal_period)
    hist = [line[i] - signal[i] for i in range(len(line))]
    return {"line": line, "signal": signal, "hist": hist}


def bollinger(prices: list[float], period: int, mult: float) -> dict:
    mid = sma(prices, period)
    sd = _rolling_std(prices, period, mid)
    upper = [mid[i] + mult * sd[i] for i in range(len(mid))]
    lower = [mid[i] - mult * sd[i] for i in range(len(mid))]
    return {"upper": upper, "mid": mid, "lower": lower}


def rolling_max(values: list[float], period: int) -> list[float]:
    out = _nan(len(values))
    for i in range(period - 1, len(values)):
        m = -math.inf
        for j in range(i - period + 1, i + 1):
            if values[j] > m:
                m = values[j]
        out[i] = m
    return out


def rolling_min(values: list[float], period: int) -> list[float]:
    out = _nan(len(values))
    for i in range(period - 1, len(values)):
        m = math.inf
        for j in range(i - period + 1, i + 1):
            if values[j] < m:
                m = values[j]
        out[i] = m
    return out


def donchian(s: dict, period: int) -> dict:
    return {"upper": rolling_max(s["high"], period), "lower": rolling_min(s["low"], period)}


def stochastic(s: dict, period: int, smooth_k: int, smooth_d: int) -> dict:
    close = s["close"]
    n = len(close)
    hh = rolling_max(s["high"], period)
    ll = rolling_min(s["low"], period)
    raw_k = _nan(n)
    for i in range(period - 1, n):
        rng = hh[i] - ll[i]
        raw_k[i] = 0 if rng == 0 else ((close[i] - ll[i]) / rng) * 100
    k = sma(raw_k, smooth_k)
    d = sma(k, smooth_d)
    return {"k": k, "d": d}
