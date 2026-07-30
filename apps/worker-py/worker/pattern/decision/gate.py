"""The cost filter — decides whether a bar is worth an LLM call.

This is what makes the pattern brain affordable. Without it every closed bar is an API call; on
15m that is 96 calls a day per symbol, nearly all of them answering "nothing is happening". With
it, most bars resolve for free and DeepSeek is only asked where something actually changed.

The rules, in the operator's own terms:

  * Flat, no trigger        -> no call. Nothing to decide.
  * Flat, trigger fired     -> call, provided the regime permits that side and we are not in a
                               post-trade cooldown.
  * In position, aligned    -> no call. A bullish break while already long tells us nothing.
  * In position, conflicted -> call. A regime flip or reversal pattern against an open position
                               is exactly the moment worth paying for.

Every decision returns a reason string, and every skip is logged. Knowing WHY the system stayed
out is as important as knowing why it entered — a gate that is too tight is invisible otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..features.regime import (
    BEARISH_REGIMES, BULLISH_REGIMES, STRONG_DOWNTREND, STRONG_UPTREND,
)

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

    # The one deterministic refusal kept: do not fade a STRONG trend. A weak trend or a range is
    # a judgement call and goes to the model; standing in front of a strong one is not.
    allowed = [t for t in directional if not _fights_strong_trend(t, regime_label)]
    if not allowed:
        return GateResult(
            False, "",
            f"Every trigger fades a {regime_label} — not standing in front of a strong trend.",
        )

    return GateResult(
        True, ENTRY,
        f"{regime_label} with {len(allowed)} directional trigger(s): "
        + ", ".join(t["name"] for t in allowed),
        tuple(allowed),
    )


def _fights_strong_trend(trigger: dict, regime_label: str) -> bool:
    return (
        (trigger["side"] == LONG and regime_label == STRONG_DOWNTREND)
        or (trigger["side"] == SHORT and regime_label == STRONG_UPTREND)
    )
