"""asyncpg-backed SignalStore — DB adapter for live-signal evaluation (Phase 3c-3)."""
from __future__ import annotations

from typing import Optional

import asyncpg


class PgSignalStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, execution_mode, parameters_json FROM strategies WHERE ticker_id = $1 AND status = 'live' LIMIT 1",
                ticker_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "executionMode": row["execution_mode"], "parametersJson": row["parameters_json"]}

    async def load_recent_closes(self, ticker_id: str, limit: int) -> list[float]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT close FROM ohlcv_data WHERE ticker_id = $1 ORDER BY timestamp DESC LIMIT $2", ticker_id, limit
            )
        return [float(r["close"]) for r in reversed(rows)]

    async def get_open_position_for_strategy(self, ticker_id: str, strategy_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, entry_price, unrealized_pl FROM positions "
                "WHERE ticker_id = $1 AND strategy_id = $2 AND status = 'open' LIMIT 1",
                ticker_id, strategy_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "entryPrice": row["entry_price"], "unrealizedPl": row["unrealized_pl"]}

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
