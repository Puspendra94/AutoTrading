"""DB-level interval roll-ups from the 1m base data.

We store BTCUSDT at 1m only. Any higher interval (5m, 15m, 1h, 4h, 1d, …) is derived on
demand with TimescaleDB's `time_bucket` — a proper OHLCV aggregation (first open, max high,
min low, last close, sum volume). Because it's computed in the database over the hypertable,
switching interval is cheap and needs no duplicate ingestion. If a specific interval ever
gets hot enough to need pre-materialising, the same query becomes a TimescaleDB continuous
aggregate with no change to callers.
"""
from __future__ import annotations

import re

from .db import get_pool

# Map an interval string to a Postgres interval literal for time_bucket().
_UNIT_SQL = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def interval_to_pg(interval: str) -> str:
    m = re.match(r"^(\d+)([mhdw])$", interval or "", re.IGNORECASE)
    if not m:
        raise ValueError(f"Unsupported interval: {interval}")
    return f"{int(m.group(1))} {_UNIT_SQL[m.group(2).lower()]}"


async def get_candles(ticker_id: str, interval: str = "1m", limit: int = 500) -> list[dict]:
    """Return the most recent `limit` candles for a ticker, rolled up to `interval` from
    the stored 1m base. Ascending by time."""
    # bucket is derived from a strict `^\d+[mhdw]$` regex (interval_to_pg), so inlining it
    # as an interval literal is injection-safe — and it sidesteps asyncpg wanting a
    # timedelta for a bound ::interval param.
    bucket = interval_to_pg(interval)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT bucket AS timestamp, open, high, low, close, volume,
                   quote_volume, trades, taker_buy_base, taker_buy_quote FROM (
              SELECT
                time_bucket(INTERVAL '{bucket}', timestamp) AS bucket,
                first(open, timestamp)  AS open,
                max(high)               AS high,
                min(low)                AS low,
                last(close, timestamp)  AS close,
                sum(volume)             AS volume,
                -- Flow columns are additive, so they roll up by SUM like volume. taker_buy_base
                -- summed over the bucket, divided by the bucket's volume, is the imbalance for
                -- the aggregated bar — which is why these have to be carried through the roll-up
                -- rather than computed per-minute and averaged (an average of ratios is not the
                -- ratio of the sums, and would quietly weight a thin minute like a heavy one).
                sum(quote_volume)       AS quote_volume,
                sum(trades)             AS trades,
                sum(taker_buy_base)     AS taker_buy_base,
                sum(taker_buy_quote)    AS taker_buy_quote
              FROM ohlcv_data
              WHERE ticker_id = $1
              GROUP BY bucket
              ORDER BY bucket DESC
              LIMIT $2
            ) t
            ORDER BY timestamp ASC
            """,
            ticker_id, limit,
        )
    return [dict(r) for r in rows]
