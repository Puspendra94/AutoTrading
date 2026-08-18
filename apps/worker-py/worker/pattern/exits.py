"""The deterministic exit ladder.

The operator's requirement, stated plainly: the stop-loss must ALWAYS execute, but the
take-profit must not close the trade — "I want to book max of profit… once it crosses TP we wait
till the next reverse trend".

Taken literally that is dangerous: a target crossed at +3% with no protective change means the
position can retrace all the way to the original stop and turn a winner into a loser. Polling for
a reversal every minute does not fix it either — BTC can give back several percent inside one
minute, between two polls.

So the target is a HANDOFF, not an exit. Crossing it:
  * does not close the position, and
  * ratchets the stop up to breakeven-plus-fees, then trails it behind the best price seen.

The profit is then free to run indefinitely — while the worst case has become "scratch" instead of
"full stop". The LLM's discretionary exit (Phase 2's exit path) sits on top and may only bring the
exit FORWARD; it can never widen a stop, cancel one, or hold past a protective level.

Ladder order, evaluated every tick:

  1. stop-loss        — absolute, checked first, always closes
  2. max hold         — a position that has gone nowhere for a day is capital doing nothing
  3. target crossed   — ratchet to breakeven+fees, switch to 'runner'. Does NOT close.
  4. give-back        — a PRE-target winner that has surrendered too much of its peak. Closes.
  5. trailing (runner) — ratchet only, never widens

Step 4 exists because steps 1-3 left a hole: below the target the only exits were the stop and max
hold, so a trade that ran most of the way to its target and then reversed handed back every point
and stopped out. "The target is a soft handoff" was true of the target, but with nothing else able
to close a position it still behaved as a hard gate — profit did not influence the exit at all
until the target was crossed.

Pure functions over a position dict: no DB, no clock, no exchange.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from ..config import config

LONG = "long"
SHORT = "short"

OPEN = "open"
RUNNER = "runner"

# "Breakeven" that ignores costs is a small GUARANTEED loss, so the protective floor has to clear
# the full round trip: taker fee on both legs plus the spread crossed on both legs. Derived from
# the same config the execution engine charges against, rather than hardcoded, so the two can
# never drift apart — a hardcoded 0.1% was already under the real 0.12% cost of a futures round
# trip once simulated slippage started being charged.
FEE_BUFFER_PCT = 2 * (config.futures_taker_fee_pct + config.paper_slippage_pct)

# Trailing distance behind the best price seen, in ATR. Wide enough that ordinary noise inside a
# trend does not knock the position out — the point of a runner is to stay in.
TRAIL_ATR_MULTIPLE = 2.0

# 15m bars. 96 = 24 hours: past that, a position that has neither run nor stopped is capital tied
# up in a setup that did not work.
MAX_HOLD_BARS = 96

# --- Give-back: protect profit that exists but has not reached the target.
#
# The gap this fills. Before this rule the ONLY ways out below the target were the stop and max
# hold, so a trade that ran 80% of the way to its target and then rolled over gave every point
# back and exited at the stop. The target was meant to be a soft handoff, but because nothing else
# could close a position, it had become a hard gate: profit was not an input to the exit decision
# until the target was crossed.
#
# The AI exit could not cover this either — it is consulted on BAR CLOSE and only when a trigger
# opposes the position, the regime flips, or the trade is already a runner. "Up 80 points and
# stalling" is none of those, so nothing looked at it.
#
# ARM_R is what stops this firing on noise. Below half a unit of risk the "peak" is one bar's
# wiggle, and trailing it would exit almost immediately after every entry.
GIVE_BACK_ARM_R = float(os.getenv("GIVE_BACK_ARM_R", "0.5"))
# Share of the best unrealised profit that may be surrendered before closing. 0.35 means a trade
# that peaked at +80 points exits around +52.
#
# Tighter is not automatically better: peak profit is measured against a moving price, so a 10%
# give-back on a position that is up 2 ATR is triggered by well under one bar's normal range. Both
# knobs are env-tunable because the right value depends on the timeframe's noise, not on taste.
GIVE_BACK_FRACTION = float(os.getenv("GIVE_BACK_FRACTION", "0.35"))

# Actions
HOLD = "hold"
EXIT = "exit"
RATCHET = "ratchet"


@dataclass(frozen=True)
class ExitAction:
    action: str                      # 'hold' | 'exit' | 'ratchet'
    reason: str = ""
    new_stop: Optional[float] = None
    new_lifecycle: Optional[str] = None

    @property
    def is_exit(self) -> bool:
        return self.action == EXIT

    @property
    def is_ratchet(self) -> bool:
        return self.action == RATCHET


def _f(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def better_extreme(side: str, current_extreme: Optional[float], price: float) -> float:
    """The best price seen since entry — highest for a long, lowest for a short."""
    if current_extreme is None:
        return price
    return max(current_extreme, price) if side == LONG else min(current_extreme, price)


def breakeven_stop(side: str, entry: float) -> float:
    """Breakeven plus enough to cover round-trip fees, so 'protected' is not a small loss."""
    return entry * (1 + FEE_BUFFER_PCT) if side == LONG else entry * (1 - FEE_BUFFER_PCT)


def trailing_stop(side: str, extreme: float, atr: float, multiple: float = TRAIL_ATR_MULTIPLE) -> float:
    """Stop trailing `multiple` ATR behind the best price seen."""
    return extreme - atr * multiple if side == LONG else extreme + atr * multiple


def is_better_stop(side: str, candidate: float, current: Optional[float]) -> bool:
    """Ratchet-only: a stop may move toward profit, never away from it.

    This is the single most important invariant in the module. A stop that can widen is not a
    stop — one bad tick or a stale ATR would quietly hand back the protection the trade already
    earned.
    """
    if current is None:
        return True
    return candidate > current if side == LONG else candidate < current


def stop_hit(side: str, price: float, stop: Optional[float]) -> bool:
    if stop is None:
        return False
    return price <= stop if side == LONG else price >= stop


def target_reached(side: str, price: float, target: Optional[float]) -> bool:
    if target is None:
        return False
    return price >= target if side == LONG else price <= target


def evaluate_exit(position: dict, price: float, atr: float, bars_held: int = 0) -> ExitAction:
    """One tick of the ladder. `position` needs side, entryPrice, stopLoss, takeProfit,
    lifecycle and extremePrice."""
    side = position.get("side") or LONG
    entry = _f(position.get("entryPrice"))
    stop = position.get("stopLoss")
    stop = _f(stop) if stop is not None else None
    target = position.get("takeProfit")
    target = _f(target) if target is not None else None
    lifecycle = position.get("lifecycle") or OPEN
    extreme = better_extreme(side, position.get("extremePrice") and _f(position["extremePrice"]), price)

    # 1. Stop-loss. First, unconditional, and it closes — nothing below may override it.
    if stop_hit(side, price, stop):
        return ExitAction(EXIT, f"Stop-loss hit at {price:.2f} (stop {stop:.2f}).")

    # 2. Max hold.
    if bars_held >= MAX_HOLD_BARS:
        return ExitAction(EXIT, f"Max hold reached ({bars_held} bars).")

    # 3. Target crossed -> hand off to the runner. Deliberately does NOT close: the operator wants
    # profit to run. Protection is what changes, not the position.
    if lifecycle == OPEN and target_reached(side, price, target):
        floor = breakeven_stop(side, entry)
        trail = trailing_stop(side, extreme, atr) if atr > 0 else floor
        # Whichever protects more. Early in a fast move the trail can still sit below breakeven,
        # and accepting it there would leave the trade able to close for a loss after tagging its
        # target — the exact outcome this ladder exists to prevent.
        candidate = max(floor, trail) if side == LONG else min(floor, trail)
        new_stop = candidate if is_better_stop(side, candidate, stop) else stop
        return ExitAction(
            RATCHET,
            f"Target {target:.2f} reached at {price:.2f} — letting it run, stop raised to "
            f"{new_stop:.2f} (breakeven+fees {floor:.2f}).",
            new_stop=new_stop, new_lifecycle=RUNNER,
        )

    # 4. Give-back, BEFORE the target is reached. Deliberately restricted to lifecycle 'open':
    # once a trade is a RUNNER it is past its target and the ATR trail already protects it, and
    # running both would mean two different rules deciding the same exit.
    if lifecycle == OPEN:
        initial_stop = _f(position.get("initialStop"), entry)
        level = give_back_exit(side, entry, initial_stop, extreme, price)
        if level is not None:
            peak = abs(extreme - entry)
            return ExitAction(
                EXIT,
                f"Gave back {GIVE_BACK_FRACTION * 100:.0f}% of a {peak:.2f} peak "
                f"(best {extreme:.2f}, floor {level:.2f}) at {price:.2f}.",
            )

    # 5. Trailing, once running.
    if lifecycle == RUNNER and atr > 0:
        candidate = trailing_stop(side, extreme, atr)
        if is_better_stop(side, candidate, stop):
            return ExitAction(
                RATCHET,
                f"Trailing stop to {candidate:.2f} ({TRAIL_ATR_MULTIPLE:.1f} ATR behind {extreme:.2f}).",
                new_stop=candidate, new_lifecycle=RUNNER,
            )

    return ExitAction(HOLD)


def give_back_exit(
    side: str, entry: float, initial_stop: float, extreme: float, price: float,
    arm_r: float = GIVE_BACK_ARM_R, fraction: float = GIVE_BACK_FRACTION,
) -> Optional[float]:
    """The price at which a pre-target winner has given back too much, or None.

    Returns the give-back LEVEL when `price` has breached it, so the caller can report the number
    rather than just the verdict. Symmetric for shorts: "profit" is always measured in the
    direction of the trade.
    """
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None
    peak_profit = (extreme - entry) if side == LONG else (entry - extreme)
    if peak_profit <= 0 or (peak_profit / risk) < arm_r:
        return None          # not enough profit yet for there to be anything worth protecting
    keep = peak_profit * (1.0 - fraction)
    level = entry + keep if side == LONG else entry - keep
    breached = price <= level if side == LONG else price >= level
    return level if breached else None


def r_multiple(position: dict, exit_price: float) -> float:
    """Result in R — multiples of what the trade originally risked.

    Measured against `initialStop`, never the ratcheted one: a runner that trails up to +3R and
    closes there did not make 0R just because its stop had moved to breakeven.
    """
    side = position.get("side") or LONG
    entry = _f(position.get("entryPrice"))
    initial_stop = _f(position.get("initialStop"))
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return 0.0
    move = (exit_price - entry) if side == LONG else (entry - exit_price)
    return move / risk
