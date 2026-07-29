"""Deterministic feature engine for the pattern brain.

Layers, bottom up:

  arrays      -> ohlcv lists into the float64 numpy arrays TA-Lib wants
  indicators  -> TA-Lib: EMA/RSI/ATR/ADX/volume + the candlestick (CDL*) patterns
  pivots      -> swing highs/lows (pure numpy fractals — no scipy)
  levels      -> support/resistance clustered from pivots, strength = touch count
  patterns    -> chart geometry over pivots (double top/bottom, H&S, triangles, range)
  regime      -> one label for "what kind of market is this"
  triggers    -> named events that are worth spending an LLM call on
  state       -> composes all of the above into one FeatureState

Everything here is a pure function of a candle list. No DB, no network, no LLM, no clock —
so the whole engine can be tested on stored candles and replayed identically.
"""
from .state import FeatureState, build_feature_state

__all__ = ["FeatureState", "build_feature_state"]
