"""Pure-function tests for the archive parser (no DB/network)."""
from datetime import date, datetime, timezone

import pytest

from worker.binance_archive import _months, _to_utc
from worker.candles import interval_to_pg


def test_to_utc_milliseconds():
    # 2021-01-01T00:00:00Z in ms
    assert _to_utc("1609459200000") == datetime(2021, 1, 1, tzinfo=timezone.utc)


def test_to_utc_microseconds_scaled():
    # Same instant expressed in microseconds (Binance 2025+ archive format)
    assert _to_utc("1609459200000000") == datetime(2021, 1, 1, tzinfo=timezone.utc)


def test_months_inclusive_range_and_wrap():
    months = _months("2023-11", date(2024, 2, 15))
    assert months == [(2023, 11), (2023, 12), (2024, 1), (2024, 2)]


def test_interval_to_pg():
    assert interval_to_pg("1m") == "1 minutes"
    assert interval_to_pg("15m") == "15 minutes"
    assert interval_to_pg("4h") == "4 hours"
    assert interval_to_pg("1d") == "1 days"
    with pytest.raises(ValueError):
        interval_to_pg("bogus")
