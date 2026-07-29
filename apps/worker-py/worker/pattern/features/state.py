"""FeatureState — every layer composed into the one object the rest of the brain consumes.

`to_state_pack()` is deliberately the ONLY place the LLM-facing shape is defined. Phase 2's
prompt builds from it, the UI renders from it, and a logged decision stores it verbatim, so a
past decision can be replayed against exactly the numbers that produced it.

NaN is scrubbed to None on the way out: NaN is not valid JSON, `json.dumps` emits a bare `NaN`
that strict parsers reject, and it would reach the LLM as a literal. None reads as "not
available yet", which is what a warming-up indicator actually means.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .arrays import Candles, to_arrays
from .indicators import MIN_BARS, Indicators, compute_indicators, detect_candle_patterns, summarize
from .levels import build_levels
from .patterns import detect_chart_patterns
from .pivots import Pivot, find_pivots, structure
from .regime import classify_regime, regime_bias
from .triggers import detect_triggers


def _clean(value: Any) -> Any:
    """Recursively replace NaN/Inf with None so the result is strict-JSON serialisable."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass(frozen=True)
class FeatureState:
    ticker_id: str
    symbol: str
    interval: str
    bar_time_ms: int
    close: float
    warm: bool  # False while indicators are still filling — nothing downstream should act
    candles: Candles
    indicators: Indicators
    pivots: list[Pivot]
    summary: dict
    regime: dict
    structure: dict
    levels: dict
    chart_patterns: list[dict]
    candle_patterns: list[dict]
    triggers: list[dict]

    @property
    def bias(self) -> str:
        """'long' | 'short' | 'none' — the side the regime permits."""
        return regime_bias(self.regime["label"])

    def to_state_pack(self) -> dict:
        """The JSON the LLM sees, the UI renders, and a decision row stores.

        Levels lead, because a level is what a stop and a target are built from — a pattern name
        alone cannot be turned into either.
        """
        return _clean({
            "market": {
                "tickerId": self.ticker_id,
                "symbol": self.symbol,
                "interval": self.interval,
                "barTime": self.bar_time_ms,
                "close": self.close,
            },
            "regime": self.regime,
            "levels": self.levels,
            "structure": self.structure,
            "patterns": {"chart": self.chart_patterns, "candle": self.candle_patterns},
            "indicators": self.summary,
            "triggers": self.triggers,
        })


def build_feature_state(
    ticker_id: str, symbol: str, interval: str, candles: list[dict]
) -> FeatureState:
    """Run every feature layer over `candles` (ascending, oldest first).

    Short history is not an error — it is the normal state after a fresh backfill. The state
    comes back with `warm=False` and empty triggers, so callers can render what exists without
    a special case, and nothing acts on half-formed indicators.
    """
    c = to_arrays(candles)
    ind = compute_indicators(c)
    warm = len(c) >= MIN_BARS and ind.has_warmup

    summary = summarize(c, ind)
    regime = classify_regime(c, ind)
    pivots = find_pivots(c)
    atr = summary["atr14"]
    levels = build_levels(c, pivots, atr)
    chart_patterns = detect_chart_patterns(pivots, atr)
    candle_patterns = detect_candle_patterns(c)
    # Triggers are the only layer gated on warm-up: acting on a break of a level derived from a
    # NaN ATR is exactly the sort of thing that looks fine until it places an order.
    triggers = detect_triggers(c, ind, levels, chart_patterns, regime) if warm else []

    return FeatureState(
        ticker_id=ticker_id,
        symbol=symbol,
        interval=interval,
        bar_time_ms=int(c.time_ms[-1]),
        close=float(c.close[-1]),
        warm=warm,
        candles=c,
        indicators=ind,
        pivots=pivots,
        summary=summary,
        regime=regime,
        structure=structure(pivots),
        levels=levels,
        chart_patterns=chart_patterns,
        candle_patterns=candle_patterns,
        triggers=triggers,
    )
