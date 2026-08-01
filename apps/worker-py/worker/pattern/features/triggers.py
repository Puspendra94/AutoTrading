"""Triggers — the named events that are worth spending an LLM call on.

This is the cost filter, and it is the whole reason the pattern brain is affordable. Without it
every closed candle would be an API call; with it, most candles resolve to "nothing happened"
for free, and DeepSeek is only asked about bars where something actually did.

A trigger says only "something changed here, worth a look". It is NOT a trade signal — it names
no size, no stop, no entry. Deciding what to do about it is the LLM's job, and vetoing it is the
guardrails' job.

Every trigger carries the `side` it would imply, so the gate can compare it against an open
position and skip the call when they already agree (a bullish break while long tells us nothing
we don't know).
"""
from __future__ import annotations

import math

from .arrays import Candles
from .indicators import Indicators
from .regime import BEARISH_REGIMES, BULLISH_REGIMES, classify_regime

# How close price must come to a level to count as a pullback/test rather than a breakout.
PULLBACK_PROXIMITY_ATR = 0.5
# A break must clear the level by this much to be a break and not a wick brushing it.
BREAK_MARGIN_ATR = 0.1
# Volume multiple over its 20-bar average that marks a break as "confirmed".
VOLUME_CONFIRM_MULTIPLE = 1.2

LONG = "long"
SHORT = "short"


def _confirmed_by_volume(c: Candles, ind: Indicators) -> bool:
    vol_ma = float(ind.volume_ma[-1]) if ind.volume_ma.size else float("nan")
    if math.isnan(vol_ma) or vol_ma <= 0:
        return False
    return float(c.volume[-1]) >= vol_ma * VOLUME_CONFIRM_MULTIPLE


def detect_triggers(
    c: Candles, ind: Indicators, levels: dict, chart_patterns: list[dict], regime: dict
) -> list[dict]:
    """Events completed by the bar that just closed. Returns [{name, side, price, detail}]."""
    if len(c) < 2:
        return []

    atr = float(ind.atr[-1]) if ind.atr.size else float("nan")
    if math.isnan(atr) or atr <= 0:
        return []

    close = float(c.close[-1])
    prev_close = float(c.close[-2])
    margin = atr * BREAK_MARGIN_ATR
    volume_ok = _confirmed_by_volume(c, ind)
    found: list[dict] = []

    # --- Level breaks. Compared against the PREVIOUS close so this fires once, on the bar that
    # crossed, rather than every bar for as long as price stays on the far side.
    for level in levels.get("resistance", []):
        price = level["price"]
        if prev_close <= price and close > price + margin:
            found.append({
                "name": "resistance_break", "side": LONG, "price": price,
                "detail": f"close {close:.2f} broke resistance {price:.2f} "
                          f"({level['touches']} touches){', volume confirmed' if volume_ok else ''}",
            })
            break  # only the nearest broken level is news
    for level in levels.get("support", []):
        price = level["price"]
        if prev_close >= price and close < price - margin:
            found.append({
                "name": "support_break", "side": SHORT, "price": price,
                "detail": f"close {close:.2f} broke support {price:.2f} "
                          f"({level['touches']} touches){', volume confirmed' if volume_ok else ''}",
            })
            break

    # --- Pullbacks INTO a level while the trend still points the other way. This is the
    # "buy the dip in an uptrend" setup, and it is deliberately separate from a break: it fires
    # while price is approaching a level it has NOT broken.
    label = regime.get("label")
    if label in BULLISH_REGIMES:
        for level in levels.get("support", []):
            if level["distAtr"] <= PULLBACK_PROXIMITY_ATR:
                found.append({
                    "name": "pullback_to_support", "side": LONG, "price": level["price"],
                    "detail": f"{label} pulling back to support {level['price']:.2f} "
                              f"({level['distAtr']:.2f} ATR away, {level['touches']} touches)",
                })
                break
    elif label in BEARISH_REGIMES:
        for level in levels.get("resistance", []):
            if level["distAtr"] <= PULLBACK_PROXIMITY_ATR:
                found.append({
                    "name": "pullback_to_resistance", "side": SHORT, "price": level["price"],
                    "detail": f"{label} rallying into resistance {level['price']:.2f} "
                              f"({level['distAtr']:.2f} ATR away, {level['touches']} touches)",
                })
                break

    # --- Range breakout: price leaving the outermost confirmed structure entirely.
    range_high, range_low = levels.get("rangeHigh"), levels.get("rangeLow")
    if range_high is not None and prev_close <= range_high and close > range_high + margin:
        found.append({"name": "range_breakout_up", "side": LONG, "price": range_high,
                      "detail": f"close {close:.2f} left the range above {range_high:.2f}"})
    if range_low is not None and prev_close >= range_low and close < range_low - margin:
        found.append({"name": "range_breakout_down", "side": SHORT, "price": range_low,
                      "detail": f"close {close:.2f} left the range below {range_low:.2f}"})

    # --- A directional chart pattern completing.
    #
    # Several of these can fire on one bar pointing opposite ways — the shapes are matched against
    # different pivots, so a double top (highs) and an inverse head & shoulders (lows) can both be
    # real at once. The `strength` carried through here is what lets the decision layer prefer one
    # instead of throwing the bar away as contradictory.
    for pattern in chart_patterns:
        if pattern["direction"] == "neutral":
            continue  # a symmetrical triangle says nothing until an edge breaks
        strength = pattern.get("strength")
        detail = f"{pattern['name']} ({pattern['direction']})"
        if strength is not None:
            detail += (
                f", strength {strength:.2f}"
                f" [{pattern.get('prominenceAtr', 0):.2f} ATR tall,"
                f" completed {pattern.get('barsSinceCompletion', 0)} bars ago]"
            )
        found.append({
            "name": "pattern_complete",
            "side": LONG if pattern["direction"] == "bullish" else SHORT,
            "price": pattern.get("level", close),
            "detail": detail,
            "strength": strength,
        })

    # --- Regime flip: the market changed character on this bar. Matters most when a position is
    # open against the new direction, which is exactly when the gate should pay for a decision.
    previous = classify_regime(c, ind, at=-2)
    if previous["label"] != label:
        if label in BULLISH_REGIMES and previous["label"] not in BULLISH_REGIMES:
            found.append({"name": "regime_flip", "side": LONG, "price": close,
                          "detail": f"regime {previous['label']} -> {label}"})
        elif label in BEARISH_REGIMES and previous["label"] not in BEARISH_REGIMES:
            found.append({"name": "regime_flip", "side": SHORT, "price": close,
                          "detail": f"regime {previous['label']} -> {label}"})

    return found
