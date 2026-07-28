"""asyncpg-backed SignalStore — DB adapter for live-signal evaluation (Phase 3c-3)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import asyncpg

# Mirror of MarketDataService.PG_UNIT — maps an interval suffix to the TimescaleDB time_bucket unit.
_PG_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_INTERVAL_RE = re.compile(r"^(\d+)([mhdw])$", re.IGNORECASE)


def _rows_to_candles(rows) -> list[dict]:
    """Map DB rows (ASC order already) to the interpreter's candle dicts with ms timestamps."""
    return [
        {"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
         "close": float(r["close"]), "volume": float(r["volume"]),
         "timestamp": int(r["timestamp"].timestamp() * 1000)}
        for r in rows
    ]


class PgSignalStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, execution_mode, parameters_json, eval_interval FROM strategies "
                "WHERE ticker_id = $1 AND status = 'live' LIMIT 1",
                ticker_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "executionMode": row["execution_mode"],
                "parametersJson": row["parameters_json"], "evalInterval": row["eval_interval"]}

    async def load_recent_closes(self, ticker_id: str, limit: int) -> list[float]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT close FROM ohlcv_data WHERE ticker_id = $1 ORDER BY timestamp DESC LIMIT $2", ticker_id, limit
            )
        return [float(r["close"]) for r in reversed(rows)]

    async def load_recent_candles(self, ticker_id: str, limit: int) -> list[dict]:
        """OHLC candles (ASC, ms timestamps) for the DSL interpreter — RAW 1m base. Use
        load_recent_candles_for_interval for the live-signal path so a higher-timeframe strategy
        isn't evaluated on 1m noise."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT timestamp, open, high, low, close, volume FROM ohlcv_data "
                "WHERE ticker_id = $1 ORDER BY timestamp DESC LIMIT $2",
                ticker_id, limit,
            )
        return _rows_to_candles(list(reversed(rows)))

    async def load_recent_candles_for_interval(self, ticker_id: str, interval: str, limit: int) -> list[dict]:
        """OHLC candles aggregated to `interval` (ASC, ms timestamps), mirroring the backend's
        MarketDataService.getCandlesForInterval time_bucket roll-up (first open / max high / min low
        / last close / sum volume) so the worker evaluates a strategy on the SAME bars it was
        backtested/promoted on. `1m` short-circuits to the raw rows."""
        if interval == "1m":
            return await self.load_recent_candles(ticker_id, limit)
        m = _INTERVAL_RE.match(interval or "")
        if not m:
            raise ValueError(f"Unsupported interval: {interval}")
        bucket = f"{int(m.group(1))} {_PG_UNIT[m.group(2).lower()]}"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT "timestamp", open, high, low, close, volume FROM (
                       SELECT time_bucket(INTERVAL '{bucket}', "timestamp") AS "timestamp",
                         first(open, "timestamp") AS open, max(high) AS high, min(low) AS low,
                         last(close, "timestamp") AS close, sum(volume) AS volume
                       FROM ohlcv_data WHERE ticker_id = $1
                       GROUP BY 1 ORDER BY 1 DESC LIMIT $2
                     ) t ORDER BY "timestamp" ASC""",
                ticker_id, limit,
            )
        return _rows_to_candles(rows)

    async def save_signals(self, strategy_id: str, ticker_id: str, interval: str,
                           direction: str, signals: list[dict]) -> None:
        """Idempotently persist a replay's signals. The unique key (strategy, interval, bar, side)
        makes re-replaying the same bars a no-op, so this can run on every request/candle close."""
        if not signals:
            return
        rows = [
            (strategy_id, ticker_id, interval,
             datetime.fromtimestamp(int(s["time"]), tz=timezone.utc),
             s["side"], direction, float(s["price"]), str(s.get("reason") or ""))
            for s in signals
        ]
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO strategy_signals
                    (strategy_id, ticker_id, interval, bar_time, side, direction, price, reason)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (strategy_id, interval, bar_time, side) DO NOTHING
                """,
                rows,
            )

    async def load_saved_signals(self, strategy_id: str, interval: str) -> list[dict]:
        """Every signal ever recorded for this strategy+interval, oldest first. Read back instead of
        returning the fresh replay so the answer is identical across interval switches and reloads,
        and so markers survive the candles that produced them ageing out of the replay window."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT bar_time, side, price, reason FROM strategy_signals
                   WHERE strategy_id = $1 AND interval = $2 ORDER BY bar_time ASC""",
                strategy_id, interval,
            )
        return [{"time": int(r["bar_time"].timestamp()), "side": r["side"],
                 "price": float(r["price"]), "reason": r["reason"]} for r in rows]

    async def get_open_position_for_strategy(self, ticker_id: str, strategy_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, entry_price, unrealized_pl, opened_at FROM positions "
                "WHERE ticker_id = $1 AND strategy_id = $2 AND status = 'open' LIMIT 1",
                ticker_id, strategy_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "entryPrice": row["entry_price"], "unrealizedPl": row["unrealized_pl"],
                "openedAt": int(row["opened_at"].timestamp() * 1000) if row["opened_at"] else None}

    async def get_ticker(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT symbol, interval FROM tickers WHERE id = $1", ticker_id)
        return {"symbol": row["symbol"], "interval": row["interval"]} if row else None

    async def retrieve_lessons(self, ticker_id: str, strategy_type: Optional[str], limit: int = 5) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, outcome, summary_text FROM ai_lessons_learned WHERE ticker_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                ticker_id, limit,
            )
            ticker_matches = [{"id": str(r["id"]), "outcome": r["outcome"], "summaryText": r["summary_text"]} for r in rows]
            if len(ticker_matches) >= limit or not strategy_type:
                return ticker_matches
            remaining = limit - len(ticker_matches)
            seen = {l["id"] for l in ticker_matches}
            rows2 = await conn.fetch(
                "SELECT id, outcome, summary_text FROM ai_lessons_learned WHERE strategy_type = $1 AND ticker_id != $2 "
                "ORDER BY created_at DESC LIMIT $3",
                strategy_type, ticker_id, remaining + len(seen),
            )
        broader = [{"id": str(r["id"]), "outcome": r["outcome"], "summaryText": r["summary_text"]}
                   for r in rows2 if str(r["id"]) not in seen]
        return (ticker_matches + broader)[:limit]
