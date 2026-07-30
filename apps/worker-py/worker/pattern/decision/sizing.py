"""Position sizing from a risk budget.

The two rules, as agreed:

  1. DAILY:     the whole day may lose at most `daily_risk_pct` (2%) of the balance the day
                started with — the previous day's close, snapshotted at 00:00 UTC.
  2. PER TRADE: the stop may not sit further than `max_stop_pct` (2%) from entry.

Rule 2 is a STOP-DISTANCE cap, not a size rule. Loss = notional x stop-distance-%, so on $20 at
a 2% stop you lose $0.40 regardless of what $20 buys. It bounds where the stop can go; it does
not bound the position.

Size comes from rule 1. The daily budget is divided by `trades_per_day` (4), so four losing
trades stop the day rather than one. Sizing the full balance into every trade — the literal
reading of "invest everything, stop at 2%" — makes a single loss consume the entire daily
budget, and the system would sit idle until 00:00 UTC after one bad entry.

    R_day    = daily_risk_pct x day_start_balance
    R_trade  = min(R_day / trades_per_day, remaining_today)
    stop_pct = clamp(requested, max(min_stop_pct, min_stop_atr x atr_pct), max_stop_pct)
    notional = R_trade / stop_pct              <- capped by max_notional_pct and free balance
    qty      = round_down_to_step(notional / entry)

The stop-distance floor is what stops a tight stop exploding the position: at a $10 stop on
BTC, R_trade / 0.0002 is an absurd notional. Clamping the stop and then capping the notional
means a too-tight stop produces an UNDER-risked trade, which is the safe direction to fail.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ...exchange_info import SymbolFilters

# --- Policy constants. These are the numbers agreed with the operator; they are deliberately
# module-level rather than LLM-supplied, because the LLM must never be able to widen its own
# risk budget by asking.
DAILY_RISK_PCT = 0.02          # rule 1: 2% of the day-start balance
TRADES_PER_DAY = 4             # four losing trades stop the day
MAX_STOP_PCT = 0.02            # rule 2: the stop may not exceed 2% from entry
MIN_STOP_PCT = 0.003           # absolute floor, so a hair-tight stop cannot explode the size
MIN_STOP_ATR = 0.5             # ...and at least half an ATR, so the stop clears normal noise
MAX_NOTIONAL_PCT = 0.50        # never commit more than half the balance to one position


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    quantity: float = 0.0
    notional: float = 0.0
    stop_price: float = 0.0
    stop_pct: float = 0.0
    risk_usd: float = 0.0            # what this trade actually loses if the stop hits
    risk_budget_usd: float = 0.0     # what it was allowed to risk
    remaining_today_usd: float = 0.0
    reason: str = ""                 # populated when approved is False
    clamped: bool = False            # True when the requested stop was moved

    def as_dict(self) -> dict:
        return {
            "approved": self.approved, "quantity": self.quantity, "notional": self.notional,
            "stopPrice": self.stop_price, "stopPct": self.stop_pct, "riskUsd": self.risk_usd,
            "riskBudgetUsd": self.risk_budget_usd, "remainingTodayUsd": self.remaining_today_usd,
            "reason": self.reason, "clamped": self.clamped,
        }


def _reject(reason: str, **kw) -> SizingResult:
    return SizingResult(approved=False, reason=reason, **kw)


def clamp_stop_pct(requested_stop_pct: float, atr_pct: float) -> tuple[float, bool]:
    """Bring a requested stop distance inside policy. Returns (stop_pct, was_clamped).

    `atr_pct` is ATR as a fraction of price (0.0035 = 0.35%), so the floor scales with
    volatility instead of being a fixed number that is too tight in a fast market and too wide
    in a quiet one.
    """
    floor = MIN_STOP_PCT
    if atr_pct and not math.isnan(atr_pct) and atr_pct > 0:
        floor = max(floor, MIN_STOP_ATR * atr_pct)
    clamped = min(max(requested_stop_pct, floor), MAX_STOP_PCT)
    return clamped, not math.isclose(clamped, requested_stop_pct, rel_tol=1e-9)


def size_position(
    *,
    side: str,
    entry_price: float,
    requested_stop_price: float,
    atr_pct: float,
    day_start_balance: float,
    realized_loss_today: float,
    free_balance: float,
    filters: SymbolFilters,
    open_notional: float = 0.0,
) -> SizingResult:
    """Size one entry. `realized_loss_today` is a POSITIVE number of dollars already lost."""
    if entry_price <= 0:
        return _reject("Entry price must be positive.")
    if day_start_balance <= 0:
        return _reject("Day-start balance is zero — nothing to risk.")
    if side not in ("long", "short"):
        return _reject(f"Unknown side '{side}'.")

    # --- Budget.
    r_day = DAILY_RISK_PCT * day_start_balance
    remaining = max(r_day - max(realized_loss_today, 0.0), 0.0)
    if remaining <= 0:
        return _reject(
            f"Daily risk budget exhausted: {r_day:.2f} USD allowed, {realized_loss_today:.2f} lost.",
            risk_budget_usd=0.0, remaining_today_usd=0.0,
        )
    r_trade = min(r_day / TRADES_PER_DAY, remaining)

    # --- Stop distance, clamped into policy.
    requested_pct = abs(entry_price - requested_stop_price) / entry_price
    if requested_pct <= 0:
        return _reject("Stop price equals entry — no risk distance to size against.",
                       risk_budget_usd=r_trade, remaining_today_usd=remaining)
    stop_pct, clamped = clamp_stop_pct(requested_pct, atr_pct)
    # Re-derive the stop PRICE from the clamped distance so the number that gets stored and
    # enforced is the one that was actually sized against.
    stop_price = entry_price * (1 - stop_pct) if side == "long" else entry_price * (1 + stop_pct)

    # --- Notional, then caps. A tighter stop demands a LARGER notional to lose the same dollars,
    # which is why the caps below matter more than the budget most of the time.
    notional = r_trade / stop_pct
    notional = min(notional, MAX_NOTIONAL_PCT * day_start_balance, max(free_balance, 0.0) + open_notional)
    if notional <= 0:
        return _reject("No capital available to allocate.",
                       risk_budget_usd=r_trade, remaining_today_usd=remaining)

    # --- Quantity the exchange will actually accept.
    quantity = filters.round_quantity(notional / entry_price)
    tradeable, why = filters.is_tradeable(quantity, entry_price)
    if not tradeable:
        return _reject(
            f"{why}. Risk budget {r_trade:.2f} USD at a {stop_pct * 100:.2f}% stop only supports "
            f"{notional:.2f} USDT of notional — fund the account or widen the stop.",
            stop_price=stop_price, stop_pct=stop_pct,
            risk_budget_usd=r_trade, remaining_today_usd=remaining, clamped=clamped,
        )

    final_notional = quantity * entry_price
    return SizingResult(
        approved=True,
        quantity=quantity,
        notional=final_notional,
        stop_price=stop_price,
        stop_pct=stop_pct,
        # Recomputed from the ROUNDED quantity, so the logged risk is what the position can
        # actually lose rather than what was requested before rounding.
        risk_usd=final_notional * stop_pct,
        risk_budget_usd=r_trade,
        remaining_today_usd=remaining,
        clamped=clamped,
    )


def take_profit_for(side: str, entry_price: float, stop_price: float, min_rr: float) -> float:
    """The take-profit that achieves `min_rr` reward:risk against the sized stop.

    A soft target, not a cap — crossing it hands the position to the trailing exit rather than
    closing it (Phase 3).
    """
    risk_distance = abs(entry_price - stop_price)
    return (entry_price + risk_distance * min_rr) if side == "long" else (entry_price - risk_distance * min_rr)


def validate_risk_reward(
    side: str, entry_price: float, stop_price: float, target_price: Optional[float], min_rr: float
) -> tuple[bool, float, str]:
    """Check a proposed target against the minimum reward:risk. Returns (ok, rr, reason)."""
    risk_distance = abs(entry_price - stop_price)
    if risk_distance <= 0:
        return False, 0.0, "Stop equals entry — reward:risk is undefined."
    if target_price is None:
        return False, 0.0, "No take-profit proposed."

    reward = (target_price - entry_price) if side == "long" else (entry_price - target_price)
    if reward <= 0:
        return False, 0.0, f"Take-profit {target_price:.2f} is on the wrong side of entry for a {side}."

    rr = reward / risk_distance
    if rr < min_rr:
        return False, rr, f"Reward:risk {rr:.2f} is below the {min_rr:.2f} minimum."
    return True, rr, ""
