"""Local intent detection — notice a move starting, without waiting for the 15m close.

THE PROBLEM THIS SOLVES
The decision loop runs on 15m bar closes. If price breaks a level three minutes into a bar, the
system does not look at it for another twelve — by which time the move has happened and the entry
is late. That lateness is measurable: signed forward return from an entry is NEGATIVE over the
first several bars at every timeframe tested, i.e. price moves against the entry before anything
works. Reacting sooner is the obvious lever.

WHY NOT JUST DECIDE ON 1m
Measured over six months, moving the whole decision loop to 1m is far worse, not better:

    15m   -0.045R    9,844 trades     net  -52,816
    5m    -0.110R   28,897 trades     net -198,654
    1m    -0.120R  126,876 trades     net -768,836

Costs rise (fee burden goes 0.08R -> 0.31R of risk per round trip) and signal quality falls with
them. So the timeframe stays at 15m; only the WAKE-UP moves to 1m.

WHAT THIS IS
A free, deterministic watcher. It runs on every 1m close, uses the levels and ATR already computed
by the last 15m evaluation, and answers one question: "has something happened that the 15m close
would have caught, but not for another N minutes?" If yes, the normal decision path runs early.
No LLM, no network, no DB — pure functions over a list of recent closes.

It decides WHEN TO LOOK, never what to do. The gate, the validator and the sizer are unchanged, so
an intent can only bring a decision forward; it cannot create one that would otherwise be refused.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

LONG = "long"
SHORT = "short"

# A cross must clear the level by this much ATR to count, so a wick brushing it is not "news".
# Mirrors BREAK_MARGIN_ATR in the 15m trigger layer deliberately: the same event should be judged
# by the same threshold whether it is seen intra-bar or at the close.
CROSS_MARGIN_ATR = float(os.getenv("INTENT_CROSS_MARGIN_ATR", "0.1"))
# Consecutive 1m closes in one direction, and how far they must travel in total, before a run
# counts as momentum rather than drift.
MOMENTUM_BARS = int(os.getenv("INTENT_MOMENTUM_BARS", "3"))
MOMENTUM_ATR = float(os.getenv("INTENT_MOMENTUM_ATR", "0.8"))
# Minimum minutes between intent-triggered decisions. Without it a single volatile stretch would
# wake the model on consecutive minutes and burn the budget on one event.
COOLDOWN_MINUTES = int(os.getenv("INTENT_COOLDOWN_MINUTES", "5"))


@dataclass(frozen=True)
class Intent:
    kind: str      # 'level_cross' | 'momentum'
    side: str      # the direction the move is going, NOT a recommendation
    price: float
    detail: str


def _levels_of(levels: dict, key: str) -> list[dict]:
    return [l for l in (levels.get(key) or []) if isinstance(l, dict) and "price" in l]


def detect_intent(closes_1m: list[float], levels: dict, atr: float) -> Optional[Intent]:
    """The strongest thing that just happened on the 1m series, or None.

    `levels` and `atr` come from the last completed 15m evaluation — the structure is 15m, only
    the observation of price against it is per-minute.
    """
    if atr <= 0 or len(closes_1m) < 2:
        return None
    prev, last = closes_1m[-2], closes_1m[-1]
    margin = atr * CROSS_MARGIN_ATR

    # 1. A 15m level crossed intra-bar. This is the case the 15m close would eventually catch,
    #    just up to fourteen minutes later — the whole reason this module exists.
    for lvl in _levels_of(levels, "resistance"):
        price = float(lvl["price"])
        if prev <= price and last > price + margin:
            return Intent("level_cross", LONG, last,
                          f"1m close {last:.2f} broke resistance {price:.2f} "
                          f"({lvl.get('touches', 0)} touches) mid-bar")
    for lvl in _levels_of(levels, "support"):
        price = float(lvl["price"])
        if prev >= price and last < price - margin:
            return Intent("level_cross", SHORT, last,
                          f"1m close {last:.2f} broke support {price:.2f} "
                          f"({lvl.get('touches', 0)} touches) mid-bar")

    # 2. A fast directional run that has not reached a level yet. Requires BOTH a monotonic
    #    sequence and a real distance travelled: three drifting closes are not a move, and one
    #    large bar is a spike rather than momentum.
    if len(closes_1m) > MOMENTUM_BARS:
        window = closes_1m[-(MOMENTUM_BARS + 1):]
        deltas = [b - a for a, b in zip(window, window[1:])]
        travelled = abs(window[-1] - window[0])
        if travelled >= atr * MOMENTUM_ATR:
            if all(d > 0 for d in deltas):
                return Intent("momentum", LONG, last,
                              f"{MOMENTUM_BARS} rising 1m closes, {travelled:.2f} "
                              f"({travelled / atr:.2f} ATR) in {MOMENTUM_BARS} minutes")
            if all(d < 0 for d in deltas):
                return Intent("momentum", SHORT, last,
                              f"{MOMENTUM_BARS} falling 1m closes, {travelled:.2f} "
                              f"({travelled / atr:.2f} ATR) in {MOMENTUM_BARS} minutes")
    return None


def cooldown_ok(now_ms: int, last_fire_ms: Optional[int]) -> bool:
    """True when enough time has passed since the last intent-triggered decision."""
    if last_fire_ms is None:
        return True
    return (now_ms - last_fire_ms) >= COOLDOWN_MINUTES * 60_000
