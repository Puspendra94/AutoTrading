"""Full-history OHLCV backfill from Binance's public archive (data.binance.vision).

Downloads monthly (and recent-daily) kline zips for a symbol/interval, parses the CSV,
and bulk-loads into the shared `ohlcv_data` Timescale hypertable — the same table the
NestJS live stream keeps current. Idempotent (ON CONFLICT DO NOTHING on the composite
`(ticker_id, timestamp)` key) and resumable month-by-month, so a long initial import of
BTCUSDT-since-2017 can be interrupted and restarted safely.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import zipfile
from datetime import date, datetime, timezone

import httpx

from .config import config
from .db import get_pool

log = logging.getLogger("worker.backfill")

# Binance switched archive kline timestamps from milliseconds to microseconds in 2025.
# Anything past this magnitude is microseconds and must be scaled down to ms.
_US_THRESHOLD = 1e14


def _to_utc(raw: str) -> datetime:
    v = int(raw)
    ms = v // 1000 if v > _US_THRESHOLD else v
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _months(start: str, end: date) -> list[tuple[int, int]]:
    sy, sm = (int(x) for x in start.split("-"))
    out: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


async def resolve_ticker_id(symbol: str, interval: str, create: bool = True) -> str | None:
    """Find the ticker row for symbol+interval, optionally creating it against the first
    available provider (the archive only backfills price data — it never touches
    credentials or trading config)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM tickers WHERE symbol = $1 AND interval = $2 LIMIT 1",
            symbol, interval,
        )
        if row:
            return str(row["id"])
        if not create:
            return None

        provider = await conn.fetchrow("SELECT id FROM providers ORDER BY created_at LIMIT 1")
        if not provider:
            log.warning("No provider row exists yet — cannot create ticker %s@%s for backfill.", symbol, interval)
            return None

        mt = await conn.fetchrow(
            "SELECT id FROM market_types WHERE provider_id = $1 AND name = 'spot' LIMIT 1",
            provider["id"],
        )
        if not mt:
            mt = await conn.fetchrow(
                "INSERT INTO market_types (provider_id, name) VALUES ($1, 'spot') RETURNING id",
                provider["id"],
            )
        new = await conn.fetchrow(
            """INSERT INTO tickers (provider_id, market_type_id, symbol, interval, status, onboarding_stage)
               VALUES ($1, $2, $3, $4, 'active', 'ready') RETURNING id""",
            provider["id"], mt["id"], symbol, interval,
        )
        log.info("Created ticker %s@%s (%s) for backfill.", symbol, interval, new["id"])
        return str(new["id"])


async def _last_stored_month(ticker_id: str) -> tuple[int, int] | None:
    """The (year, month) of the newest candle already stored for this ticker, so a restart
    resumes from there instead of re-downloading the entire history every boot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT max(timestamp) AS ts FROM ohlcv_data WHERE ticker_id = $1", ticker_id
        )
    if not row or row["ts"] is None:
        return None
    ts = row["ts"]
    return (ts.year, ts.month)


def _monthly_url(symbol: str, interval: str, y: int, m: int) -> str:
    return f"{config.binance_vision_base}/data/spot/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{y:04d}-{m:02d}.zip"


def _daily_url(symbol: str, interval: str, d: date) -> str:
    return f"{config.binance_vision_base}/data/spot/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d.isoformat()}.zip"


async def _fetch_zip_rows(client: httpx.AsyncClient, url: str) -> list[tuple] | None:
    """Returns parsed OHLCV tuples for a zip, or None if the archive file doesn't exist
    (404 is expected for not-yet-published or missing months)."""
    resp = await client.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    rows: list[tuple] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
            for r in reader:
                if not r or r[0] in ("open_time", "openTime"):  # some files carry a header
                    continue
                rows.append((_to_utc(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return rows


async def _insert_rows(ticker_id: str, rows: list[tuple]) -> int:
    if not rows:
        return 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Stage into a temp table then upsert — far faster than per-row INSERTs for the
        # tens-of-millions-of-rows 1m history, and ON CONFLICT keeps it idempotent. The
        # temp table is created inside the transaction (ON COMMIT DROP) so it survives
        # until the COPY + INSERT complete, then is cleaned up on commit.
        async with conn.transaction():
            await conn.execute(
                "CREATE TEMP TABLE _stage (ts timestamptz, o numeric, h numeric, l numeric, c numeric, v numeric) ON COMMIT DROP"
            )
            await conn.copy_records_to_table("_stage", records=rows, columns=["ts", "o", "h", "l", "c", "v"])
            result = await conn.execute(
                """INSERT INTO ohlcv_data (ticker_id, timestamp, open, high, low, close, volume)
                   SELECT $1, ts, o, h, l, c, v FROM _stage
                   ON CONFLICT (ticker_id, timestamp) DO NOTHING""",
                ticker_id,
            )
    # result like "INSERT 0 <n>"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def backfill_symbol_interval(symbol: str, interval: str) -> dict:
    """Backfill one symbol/interval's full history. Monthly zips cover everything up to
    last month; the current month is filled from daily zips."""
    ticker_id = await resolve_ticker_id(symbol, interval)
    if not ticker_id:
        return {"symbol": symbol, "interval": interval, "skipped": True, "reason": "no ticker/provider"}

    today = datetime.now(timezone.utc).date()

    # Resume from the last stored month (re-importing that month is cheap and idempotent,
    # and covers any candles that landed after the previous run stopped mid-month).
    resume = await _last_stored_month(ticker_id)
    start = f"{resume[0]:04d}-{resume[1]:02d}" if resume else config.backfill_start

    inserted = 0
    months_done = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        for (y, m) in _months(start, today):
            # Skip the current month's monthly file (not published yet) — handled by dailies.
            if (y, m) == (today.year, today.month):
                continue
            rows = await _fetch_zip_rows(client, _monthly_url(symbol, interval, y, m))
            if rows is None:
                continue
            n = await _insert_rows(ticker_id, rows)
            inserted += n
            months_done += 1
            if months_done % 12 == 0:
                log.info("%s@%s: %d months imported (%d rows so far)…", symbol, interval, months_done, inserted)

        # Current month via daily zips.
        d = today.replace(day=1)
        while d <= today:
            rows = await _fetch_zip_rows(client, _daily_url(symbol, interval, d))
            if rows:
                inserted += await _insert_rows(ticker_id, rows)
            d = date.fromordinal(d.toordinal() + 1)

    log.info("Backfill done %s@%s: %d new rows across %d months.", symbol, interval, inserted, months_done)
    return {"symbol": symbol, "interval": interval, "insertedRows": inserted, "months": months_done, "tickerId": ticker_id}


async def backfill_all() -> list[dict]:
    """Backfill every configured interval for the configured symbol (change #7)."""
    results = []
    for interval in config.backfill_intervals:
        results.append(await backfill_symbol_interval(config.backfill_symbol, interval))
    return results
