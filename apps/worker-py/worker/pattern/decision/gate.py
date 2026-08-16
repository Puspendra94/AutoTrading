"""The cost filter — decides whether a bar is worth an LLM call.

This is what makes the pattern brain affordable. Without it every closed bar is an API call; on
15m that is 96 calls a day per symbol, nearly all of them answering "nothing is happening". With
it, most bars resolve for free and DeepSeek is only asked where something actually changed.

The rules, in the operator's own terms:

  * Flat, no trigger        -> no call. Nothing to decide.
  * Flat, trigger fired     -> call, unless we are in a post-trade cooldown or the trend is
                               already extended (ADX >= 40), which is refused outright.
  * In position, aligned    -> no call. A bullish break while already long tells us nothing.
  * In position, conflicted -> call. A regime flip or reversal pattern against an open position
                               is exactly the moment worth paying for.

Every decision returns a reason string, and every skip is logged. Knowing WHY the system stayed
out is as important as knowing why it entered — a gate that is too tight is invisible otherwise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..features.regime import ADX_STRONG, BEARISH_REGIMES, BULLISH_REGIMES

# Bars to wait after a position closes before entering again on the same ticker. Stops the
# system re-entering the same failed setup on the very next bar.
COOLDOWN_BARS = 2

# Triggers that justify consulting the model about an EXIT when they oppose the open position.
REVERSAL_TRIGGERS = frozenset({
    "regime_flip", "pattern_complete", "support_break", "resistance_break",
    "range_breakout_up", "range_breakout_down",
})

ENTRY = "entry"
EXIT = "exit"

LONG = "long"
SHORT = "short"


@dataclass(frozen=True)
class GateResult:
    should_call: bool
    kind: str          # 'entry' | 'exit' | ''
    reason: str
    triggers: tuple = ()

    def as_dict(self) -> dict:
        return {"shouldCall": self.should_call, "kind": self.kind, "reason": self.reason,
                "triggers": [t["name"] for t in self.triggers]}


def _opposing(triggers: list[dict], position_side: str) -> list[dict]:
    """Triggers pointing against an open position."""
    against = "short" if position_side == "long" else "long"
    return [t for t in triggers if t["side"] == against and t["name"] in REVERSAL_TRIGGERS]


def evaluate_gate(
    state,
    open_position: Optional[dict],
    *,
    bars_since_last_close: Optional[int] = None,
    daily_budget_exhausted: bool = False,
) -> GateResult:
    """Decide whether this bar earns an LLM call. `state` is a FeatureState."""
    if not state.warm:
        return GateResult(False, "", "Feature engine is still warming up.")

    regime_label = state.regime["label"]

    # --- Position open: only a conflict is worth paying for.
    if open_position:
        side = open_position.get("side") or "long"
        opposing = _opposing(state.triggers, side)
        if opposing:
            return GateResult(
                True, EXIT,
                f"Open {side} position with {len(opposing)} opposing trigger(s): "
                + ", ".join(t["name"] for t in opposing),
                tuple(opposing),
            )
        # Regime turning against the position, even without a named trigger on this bar.
        regime_against = (
            (side == "long" and regime_label in BEARISH_REGIMES)
            or (side == "short" and regime_label in BULLISH_REGIMES)
        )
        if regime_against:
            return GateResult(True, EXIT, f"Regime is {regime_label} against an open {side}.")

        # A RUNNER is past its target with profit on the table, so it earns a look on every closed
        # bar — this is the "wait for the reversal" the operator asked for. Note it is once per
        # BAR, not the once-a-minute poll originally sketched: on 15m that is ~4% of the calls, and
        # the trailing stop (not the poll) is what actually protects the gains in between.
        if (open_position.get("lifecycle") or "open") == "runner":
            return GateResult(
                True, EXIT,
                f"Open {side} runner past its target — checking whether the move is exhausted.",
            )

        return GateResult(False, "", f"Open {side} position, market read still aligned ({regime_label}).")

    # --- Flat: an entry needs a trigger the regime agrees with.
    if daily_budget_exhausted:
        return GateResult(False, "", "Daily risk budget exhausted — no new entries today.")

    if not state.triggers:
        return GateResult(False, "", f"No trigger on this bar (regime {regime_label}).")

    if bars_since_last_close is not None and bars_since_last_close < COOLDOWN_BARS:
        return GateResult(
            False, "",
            f"Cooldown: {bars_since_last_close} of {COOLDOWN_BARS} bars since the last close.",
        )

    # A directional read — bullish or bearish — is what earns the call. The regime travels in the
    # state pack as CONTEXT for the model to weigh, it is not a veto here.
    #
    # It was a veto, and that was a mistake: requiring ADX >= 25 plus an aligned EMA stack on top
    # of the trigger meant a ranging market produced signals that could never be acted on, so the
    # system drew markers for setups it had already decided to ignore and never traded at all.
    # Judging the setup is the model's job; this gate only decides whether the question is worth
    # asking.
    directional = [t for t in state.triggers if t["side"] in (LONG, SHORT)]
    if not directional:
        return GateResult(False, "", "No directional trigger on this bar.")

    # The one deterministic refusal: no NEW ENTRY while the trend is extended, in either
    # direction. Fading a strong trend was always refused; joining one is now refused too.
    #
    # The first paper week is unambiguous about why. Five entries were taken with ADX 43-51, every
    # one of them ALIGNED with the trend (the fade was already blocked), and all five stopped out
    # for -137.41 — 54% of the total loss from 13% of the trades. Entering a move that is already
    # this extended means buying near its exhaustion point, where the first ordinary pullback takes
    # out the stop. Both sides of a strong trend are refused because both were losing bets: fading
    # it stands in front of the move, joining it arrives too late.
    #
    # This is checked on ADX directly rather than on the regime label. `strong_uptrend` also
    # requires an aligned EMA stack and slope, so a high-ADX bar with a mixed stack is labelled
    # `range` and would otherwise slip through the exact filter it should be caught by.
    #
    # Note this gates ENTRIES only. An open position is handled far above, and must always be able
    # to reach the model for an exit no matter what ADX is doing.
    adx = state.regime.get("adx")
    if adx is not None and not math.isnan(adx) and adx >= ADX_STRONG:
        return GateResult(
            False, "",
            f"ADX {adx:.1f} — the trend is already extended (>= {ADX_STRONG:.0f}); "
            "not opening into it or against it.",
        )

    return GateResult(
        True, ENTRY,
        f"{regime_label} with {len(directional)} directional trigger(s): "
        + ", ".join(t["name"] for t in directional),
        tuple(directional),
    )
