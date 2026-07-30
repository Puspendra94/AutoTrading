"""Deterministic guardrails over the model's decision.

The rule agreed with the operator: **always SKIP + log, never modify.** Silently repairing a bad
decision destroys the audit trail — the log would show a trade nobody proposed, and the next
prompt iteration would be tuned against fiction. If the model breaks a constraint, the trade does
not happen and the rejection is recorded with the model's original numbers intact.

The one exception is the stop distance, which sizing CLAMPS rather than rejects, because a stop
slightly tighter than policy is a legitimate opinion about where invalidation sits and clamping
it can only reduce risk. That clamp is recorded on the decision (`clamped`) so it is visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..features.regime import STRONG_DOWNTREND, STRONG_UPTREND
from .prompts import MIN_RISK_REWARD
from .schemas import EntryDecision
from .sizing import validate_risk_reward


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = ""
    risk_reward: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "riskReward": self.risk_reward}


def validate_entry(
    decision: EntryDecision,
    *,
    current_price: float,
    regime_label: str,
    min_rr: float = MIN_RISK_REWARD,
) -> ValidationResult:
    """Check an ENTRY decision against every hard constraint the prompt stated."""
    if decision.action != "ENTRY":
        return ValidationResult(False, "Decision is not an ENTRY.")

    side = decision.side
    if side not in ("long", "short"):
        return ValidationResult(False, f"ENTRY without a valid side (got {side!r}).")

    # --- Completeness. A missing field is a malformed decision, not a defaultable one: guessing
    # a stop the model did not choose would be inventing the most important number in the trade.
    missing = [
        name for name, value in (
            ("entry_min", decision.entry_min), ("entry_max", decision.entry_max),
            ("stop_loss", decision.stop_loss), ("take_profit", decision.take_profit),
        ) if value is None
    ]
    if missing:
        return ValidationResult(False, f"ENTRY missing required field(s): {', '.join(missing)}.")

    entry_min, entry_max = float(decision.entry_min), float(decision.entry_max)
    stop, target = float(decision.stop_loss), float(decision.take_profit)

    if entry_min > entry_max:
        return ValidationResult(False, f"entry_min {entry_min:.2f} exceeds entry_max {entry_max:.2f}.")

    # --- Trend alignment, but only against a STRONG trend.
    #
    # This used to reject any entry opposing the regime at all, which — combined with the same
    # rule in the gate — meant nothing could ever trade outside a confirmed trend. Fading a weak
    # trend or a range is a judgement call and belongs to the model; fading a STRONG one is the
    # narrow case worth refusing deterministically.
    if side == "long" and regime_label == STRONG_DOWNTREND:
        return ValidationResult(False, f"Long proposed in a {regime_label} — fading a strong trend.")
    if side == "short" and regime_label == STRONG_UPTREND:
        return ValidationResult(False, f"Short proposed in a {regime_label} — fading a strong trend.")

    # --- Fillability. Deliberately checked against the CURRENT price rather than the bar close:
    # by the time the order goes out this is the price we would actually pay.
    if not (entry_min <= current_price <= entry_max):
        return ValidationResult(
            False,
            f"Price {current_price:.2f} is outside the proposed entry range "
            f"{entry_min:.2f}-{entry_max:.2f} — the setup moved on.",
        )

    # --- Stop on the correct side of entry.
    if side == "long" and stop >= current_price:
        return ValidationResult(False, f"Long stop {stop:.2f} is not below entry {current_price:.2f}.")
    if side == "short" and stop <= current_price:
        return ValidationResult(False, f"Short stop {stop:.2f} is not above entry {current_price:.2f}.")

    # --- Reward:risk against the model's own numbers.
    ok, rr, why = validate_risk_reward(side, current_price, stop, target, min_rr)
    if not ok:
        return ValidationResult(False, why, rr)

    return ValidationResult(True, "", rr)


def validate_exit(decision, position_side: Optional[str] = None) -> ValidationResult:
    """Exit decisions carry no numbers, so there is little to check — but an unparsed or
    non-EXIT/HOLD answer must not be treated as a close."""
    action = getattr(decision, "action", None)
    if action not in ("EXIT", "HOLD"):
        return ValidationResult(False, f"Exit decision has an unknown action {action!r}.")
    return ValidationResult(True)
