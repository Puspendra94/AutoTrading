"""Structured-output schemas for the trading decision.

The LLM returns direction, an entry RANGE, a stop and a target. It never returns a quantity:
sizing is deterministic and owned by sizing.py, so there is no field through which the model
could ask for a bigger position.

An entry RANGE rather than a price is deliberate. BTC moves far enough between the decision and
the fill that a single suggested price would rarely be marketable — the range is both the fill
condition and the slippage guard.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class EntryDecision(BaseModel):
    """Whether to open a position on this bar."""

    action: Literal["ENTRY", "SKIP"] = Field(
        description="ENTRY to open a position, SKIP to do nothing this bar.",
    )
    side: Optional[Literal["long", "short"]] = Field(
        default=None, description="Direction. Required when action is ENTRY, null when SKIP.",
    )
    entry_min: Optional[float] = Field(
        default=None, description="Lowest acceptable fill price. Required for ENTRY.",
    )
    entry_max: Optional[float] = Field(
        default=None, description="Highest acceptable fill price. Required for ENTRY.",
    )
    stop_loss: Optional[float] = Field(
        default=None, description="Stop price. Must be below entry for a long, above for a short.",
    )
    take_profit: Optional[float] = Field(
        default=None, description="Target price, used as a soft target the position may run past.",
    )
    confidence: Optional[float] = Field(
        default=None, description="0-1 confidence in this setup.",
    )
    reasoning: str = Field(
        description="Short explanation citing the specific levels and regime that justify it.",
    )


class ExitDecision(BaseModel):
    """Whether to close an open position early.

    The model may only ever bring an exit FORWARD. It cannot veto a stop, and it cannot ask to
    hold past one — those are owned by the deterministic ladder.
    """

    action: Literal["EXIT", "HOLD"] = Field(
        description="EXIT to close now, HOLD to leave the position to the deterministic rules.",
    )
    reasoning: str = Field(
        description="Short explanation citing what changed in the market read.",
    )
