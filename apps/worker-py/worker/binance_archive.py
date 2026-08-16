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
import math
import zipfile
from datetime import date, datetime, timezone

import httpx

from .config import config
from .db import get_pool

# Mirrors DEFAULT_MARKET_TYPE in the backend's market-type.entity.ts. Any ticker this module
# creates must trade on the same venue the backend would have put it on.
DEFAULT_MARKET_TYPE = "futures"

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

        # Futures, not spot — see DEFAULT_MARKET_TYPE on the backend. A short is a SELL-to-open
        # that only the futures venue accepts, so a spot ticker silently makes half this system's
        # signals unexecutable.
        mt = await conn.fetchrow(
            "SELECT id FROM market_types WHERE provider_id = $1 AND name = $2 LIMIT 1",
            provider["id"], DEFAULT_MARKET_TYPE,
        )
        if not mt:
            mt = await conn.fetchrow(
                "INSERT INTO market_types (provider_id, name) VALUES ($1, $2) RETURNING id",
                provider["id"], DEFAULT_MARKET_TYPE,
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


# USD-M futures archive ("um"), not spot — the series has to be the instrument that is actually
# traded. Verified live: futures monthly data begins 2020-01; 2019-12 and earlier return 404.
_ARCHIVE_PREFIX = "data/futures/um"


def _monthly_url(symbol: str, interval: str, y: int, m: int) -> str:
    return f"{config.binance_vision_base}/{_ARCHIVE_PREFIX}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{y:04d}-{m:02d}.zip"


def _daily_url(symbol: str, interval: str, d: date) -> str:
    return f"{config.binance_vision_base}/{_ARCHIVE_PREFIX}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{d.isoformat()}.zip"


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
                # Every field Binance publishes, not just OHLCV. Columns 7-10 (quote volume,
                # trade count, taker-buy base/quote) were downloaded and discarded on every
                # backfill; taker_buy_base in particular is order-flow imbalance, which is the
                # one input here that is not another function of price. Column 6 is close_time
                # (derivable from open_time + interval) and 11 is Binance's unused "ignore".
                rows.append((
                    _to_utc(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
                    float(r[7]), int(float(r[8])), float(r[9]), float(r[10]),
                ))
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
                "CREATE TEMP TABLE _stage (ts timestamptz, o numeric, h numeric, l numeric, "
                "c numeric, v numeric, qv numeric, n integer, tbb numeric, tbq numeric) ON COMMIT DROP"
            )
            await conn.copy_records_to_table(
                "_stage", records=rows,
                columns=["ts", "o", "h", "l", "c", "v", "qv", "n", "tbb", "tbq"],
            )
            # DO UPDATE, not DO NOTHING, for the flow columns specifically: the 3.6M rows already
            # stored predate them and carry NULL. DO NOTHING would skip those rows forever, so a
            # re-backfill could never populate the history — the one thing this change exists to
            # make possible. Price/volume are left alone, since a stored bar is already correct.
            result = await conn.execute(
                """INSERT INTO ohlcv_data (ticker_id, timestamp, open, high, low, close, volume,
                                           quote_volume, trades, taker_buy_base, taker_buy_quote)
                   SELECT $1, ts, o, h, l, c, v, qv, n, tbb, tbq FROM _stage
                   ON CONFLICT (ticker_id, timestamp) DO UPDATE
                     SET quote_volume    = COALESCE(EXCLUDED.quote_volume, ohlcv_data.quote_volume),
                         trades          = COALESCE(EXCLUDED.trades, ohlcv_data.trades),
                         taker_buy_base  = COALESCE(EXCLUDED.taker_buy_base, ohlcv_data.taker_buy_base),
                         taker_buy_quote = COALESCE(EXCLUDED.taker_buy_quote, ohlcv_data.taker_buy_quote)""",
                ticker_id,
            )
    # result like "INSERT 0 <n>"
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def backfill_symbol_interval(symbol: str, interval: str, *, from_start: bool = False) -> dict:
    """Backfill one symbol/interval's full history. Monthly zips cover everything up to
    last month; the current month is filled from daily zips.

    `from_start=True` ignores the resume point and re-imports everything from
    `config.backfill_start`. Needed after a schema change that adds a column: the normal resume
    logic starts at the newest stored month, so rows already present would keep their NULLs
    forever. The upsert only fills columns that are NULL, so a full re-run is idempotent for
    price and additive for anything new.
    """
    ticker_id = await resolve_ticker_id(symbol, interval)
    if not ticker_id:
        return {"symbol": symbol, "interval": interval, "skipped": True, "reason": "no ticker/provider"}

    today = datetime.now(timezone.utc).date()

    # Resume from the last stored month (re-importing that month is cheap and idempotent,
    # and covers any candles that landed after the previous run stopped mid-month).
    resume = None if from_start else await _last_stored_month(ticker_id)
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


# ---------------------------------------------------------------------------
# Derivatives archives: funding rate and futures metrics.
#
# Different shapes from klines and from each other. Funding is MONTHLY back to 2020-01; metrics
# are DAILY only and start around 2021. Both use the same skip-on-404 rule the kline backfill
# uses, so the real start date is discovered rather than hardcoded.


def _funding_url(symbol: str, y: int, m: int) -> str:
    return (f"{config.binance_vision_base}/{_ARCHIVE_PREFIX}/monthly/fundingRate/"
            f"{symbol}/{symbol}-fundingRate-{y:04d}-{m:02d}.zip")


def _metrics_url(symbol: str, d: date) -> str:
    return (f"{config.binance_vision_base}/{_ARCHIVE_PREFIX}/daily/metrics/"
            f"{symbol}/{symbol}-metrics-{d.isoformat()}.zip")


def _num(v: str):
    """Archive rows carry empty strings for absent values. None, never 0.0 — a zero open interest
    or a zero long/short ratio is a strong claim, and a fabricated one."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if math.isnan(f) else f


async def _fetch_csv(client: httpx.AsyncClient, url: str) -> list[list[str]] | None:
    """Rows of a zipped CSV with any header dropped, or None on 404."""
    resp = await client.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    out: list[list[str]] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            for r in csv.reader(io.TextIOWrapper(fh, encoding="utf-8")):
                if not r or r[0] in ("calc_time", "create_time"):
                    continue
                out.append(r)
    return out


async def _stage_copy(table: str, cols: list[str], types: str, records: list[tuple],
                      insert_sql: str, ticker_id: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"CREATE TEMP TABLE _stg ({types}) ON COMMIT DROP")
            await conn.copy_records_to_table("_stg", records=records, columns=cols)
            res = await conn.execute(insert_sql, ticker_id)
    try:
        return int(res.split()[-1])
    except (ValueError, IndexError):
        return 0


async def backfill_funding(symbol: str, interval: str = "1m", *, start: str | None = None) -> dict:
    """Funding-rate history. calc_time is epoch MILLISECONDS; rate is a fraction (0.0001 = 0.01%)."""
    ticker_id = await resolve_ticker_id(symbol, interval)
    if not ticker_id:
        return {"symbol": symbol, "skipped": True, "reason": "no ticker"}
    today = datetime.now(timezone.utc).date()
    inserted = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        for (y, m) in _months(start or config.backfill_start, today):
            rows = await _fetch_csv(client, _funding_url(symbol, y, m))
            if not rows:
                continue
            records = [(_to_utc(r[0]), _num(r[2]), int(float(r[1])) if r[1].strip() else None)
                       for r in rows if _num(r[2]) is not None]
            if records:
                inserted += await _stage_copy(
                    "funding_rate", ["ts", "rate", "ih"],
                    "ts timestamptz, rate numeric, ih integer", records,
                    """INSERT INTO funding_rate (ticker_id, timestamp, rate, interval_hours)
                       SELECT $1, ts, rate, ih FROM _stg
                       ON CONFLICT (ticker_id, timestamp) DO NOTHING""",
                    ticker_id)
    log.info("Funding backfill %s: %d rows.", symbol, inserted)
    return {"symbol": symbol, "insertedRows": inserted, "tickerId": ticker_id}


async def backfill_metrics(symbol: str, interval: str = "1m", *, start: str | None = None) -> dict:
    """Open interest and long/short ratios at 5-minute grain, from DAILY archives only.

    404s are normal both before the series begins and for today, so they are skipped rather than
    treated as failures — which is also how the real start date gets discovered.
    """
    ticker_id = await resolve_ticker_id(symbol, interval)
    if not ticker_id:
        return {"symbol": symbol, "skipped": True, "reason": "no ticker"}
    d = datetime.strptime(start or "2021-01", "%Y-%m").date().replace(day=1)
    today = datetime.now(timezone.utc).date()
    inserted, days = 0, 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        while d <= today:
            rows = await _fetch_csv(client, _metrics_url(symbol, d))
            d = date.fromordinal(d.toordinal() + 1)
            if not rows:
                continue
            records = []
            for r in rows:
                if len(r) < 8:
                    continue
                ts = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                records.append((ts, _num(r[2]), _num(r[3]), _num(r[4]),
                                _num(r[5]), _num(r[6]), _num(r[7])))
            if not records:
                continue
            inserted += await _stage_copy(
                "futures_metrics", ["ts", "oi", "oiv", "tra", "trp", "gra", "tbs"],
                "ts timestamptz, oi numeric, oiv numeric, tra numeric, trp numeric, "
                "gra numeric, tbs numeric", records,
                """INSERT INTO futures_metrics (ticker_id, timestamp, open_interest,
                       open_interest_value, toptrader_ratio_accounts, toptrader_ratio_positions,
                       global_ratio_accounts, taker_buy_sell_ratio)
                   SELECT $1, ts, oi, oiv, tra, trp, gra, tbs FROM _stg
                   ON CONFLICT (ticker_id, timestamp) DO NOTHING""",
                ticker_id)
            days += 1
            if days % 300 == 0:
                log.info("metrics %s: %d days, %d rows so far…", symbol, days, inserted)
    log.info("Metrics backfill %s: %d rows across %d days.", symbol, inserted, days)
    return {"symbol": symbol, "insertedRows": inserted, "days": days, "tickerId": ticker_id}
