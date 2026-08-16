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
  4. trailing (runner) — ratchet only, never widens

Pure functions over a position dict: no DB, no clock, no exchange.
"""
from __future__ import annotations

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

    # 4. Trailing, once running.
    if lifecycle == RUNNER and atr > 0:
        candidate = trailing_stop(side, extreme, atr)
        if is_better_stop(side, candidate, stop):
            return ExitAction(
                RATCHET,
                f"Trailing stop to {candidate:.2f} ({TRAIL_ATR_MULTIPLE:.1f} ATR behind {extreme:.2f}).",
                new_stop=candidate, new_lifecycle=RUNNER,
            )

    return ExitAction(HOLD)


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
