"""Startup gap-fill retry tests.

The failure these pin down was silent: on a cold boot the worker can reach the database before
the backend has run its migrations, so `backfill_all` raises UndefinedTableError. The old code
logged it and carried on — the process stayed up, never exited non-zero, so the restart policy
never fired, and the candle table stayed empty indefinitely while everything looked healthy.

  * a transient failure is retried, not swallowed;
  * a success on a later attempt leaves the process running normally;
  * exhausting the retries raises, so the container exits and gets restarted.

Delays are monkeypatched to zero — these assert the retry behaviour, not the wall clock.
"""
import asyncio

import pytest

from worker import main


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Collapse the backoff so the suite does not actually wait ~4 minutes.

    `main.asyncio` is the asyncio module itself, so the replacement has to close over the real
    sleep captured up front — calling asyncio.sleep inside it would just recurse into the patch.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _d: real_sleep(0))


class FlakyBackfill:
    """Fails `failures` times, then succeeds — the cold-boot race in miniature."""

    def __init__(self, failures: int):
        self.failures, self.calls = failures, 0

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError('relation "tickers" does not exist')
        return {"BTCUSDT": 4_700_000}


@pytest.mark.asyncio
async def test_succeeds_first_try_without_retrying(monkeypatch):
    backfill = FlakyBackfill(failures=0)
    monkeypatch.setattr(main, "backfill_all", backfill)

    await main._startup_backfill()

    assert backfill.calls == 1


@pytest.mark.asyncio
async def test_retries_until_the_schema_appears(monkeypatch):
    """The whole point: a boot that loses the race still ends up with the history."""
    backfill = FlakyBackfill(failures=3)
    monkeypatch.setattr(main, "backfill_all", backfill)

    await main._startup_backfill()

    assert backfill.calls == 4


@pytest.mark.asyncio
async def test_raises_once_retries_are_exhausted(monkeypatch):
    """Must NOT return normally — a silent return is the original bug."""
    backfill = FlakyBackfill(failures=99)
    monkeypatch.setattr(main, "backfill_all", backfill)

    with pytest.raises(RuntimeError):
        await main._startup_backfill()

    assert backfill.calls == len(main._BACKFILL_RETRY_DELAYS) + 1
