"""Candle lists -> float64 numpy arrays.

asyncpg returns numeric columns as Decimal, and TA-Lib requires contiguous float64: handing it
anything else raises `Exception: input array type is not double`. Converting in exactly one
place keeps that failure from being rediscovered in every indicator.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Candles:
    """OHLCV as parallel float64 arrays, oldest first. `time_ms` is each bar's OPEN time."""

    time_ms: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return int(self.close.size)


def _col(candles: list[dict], key: str) -> np.ndarray:
    return np.asarray([float(c[key]) for c in candles], dtype=np.float64)


def _bar_time_ms(c: dict) -> float:
    """Accept either a datetime `timestamp` (DB rows) or an int ms `timestamp` (tests/replay)."""
    ts = c["timestamp"]
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp() * 1000.0)
    return float(ts)


def to_arrays(candles: list[dict]) -> Candles:
    """Convert ascending OHLCV dicts to arrays. Raises on empty input — a caller with no
    candles has a data problem and should not get silently-empty features."""
    if not candles:
        raise ValueError("to_arrays requires at least one candle")
    return Candles(
        time_ms=np.asarray([_bar_time_ms(c) for c in candles], dtype=np.float64),
        open=_col(candles, "open"),
        high=_col(candles, "high"),
        low=_col(candles, "low"),
        close=_col(candles, "close"),
        volume=_col(candles, "volume"),
    )
