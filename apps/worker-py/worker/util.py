"""Small shared helpers with no dependencies on any trading brain.

`to_fixed` used to live in strategy/evaluator.py, which meant the execution engine, the risk
gate and the live executor all imported the strategy package just to round a number. It lives
here instead so the money path (risk gate -> execution -> orders) is importable without pulling
in the strategy/backtest machinery — which the pattern brain does not use.
"""
from __future__ import annotations

from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal


def to_fixed(x: float, digits: int) -> float:
    """Match JS `Number(x.toFixed(digits))`: round the EXACT double half toward +infinity
    (ties -> larger value), then return it as a float. Uses Decimal(x) — the exact binary
    value, not repr — so e.g. 1.005 rounds like V8 does."""
    d = Decimal(x)
    q = Decimal(1).scaleb(-digits)
    rounding = ROUND_HALF_UP if d >= 0 else ROUND_HALF_DOWN  # both = half toward +infinity
    return float(d.quantize(q, rounding=rounding))
