"""asyncpg-backed ExecutionStore — production DB adapter for the execution engine (3c-2).

Not exercised until the live loop is wired and enabled (3c-3); unit tests use a fake store.
"""
from __future__ import annotations

import logging
from typing import Optional

import asyncpg

from ..config import config

log = logging.getLogger("worker.execution.store")

API_FAILURE_KILL_THRESHOLD = config.provider_api_failure_threshold


def _resolve_credential(credential_json: dict, use_testnet: bool) -> Optional[dict]:
    """Pick the network-scoped key set (mainnet/testnet), honoring a legacy flat blob.
    Mirrors PlaintextSecretsProvider.getCredential on the backend."""
    if credential_json.get("apiKey"):  # legacy flat keys = mainnet only
        return None if use_testnet else credential_json
    slot = credential_json.get("testnet" if use_testnet else "mainnet")
    return slot if slot and slot.get("apiKey") and slot.get("apiSecret") else None


class PgExecutionStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get_ticker(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT t.id, t.symbol, t.provider_id, mt.name AS market_type
                   FROM tickers t LEFT JOIN market_types mt ON mt.id = t.market_type_id
                   WHERE t.id = $1""",
                ticker_id,
            )
        return {"id": str(row["id"]), "symbol": row["symbol"], "providerId": str(row["provider_id"]),
                "marketType": row["market_type"]} if row else None

    async def get_provider(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, trading_mode, type, use_testnet FROM providers WHERE id = $1", provider_id
            )
        if not row:
            return None
        return {"id": str(row["id"]), "name": row["name"], "tradingMode": row["trading_mode"],
                "type": row["type"], "useTestnet": row["use_testnet"]}

    async def get_credential(self, provider_id: str, use_testnet: bool = False) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT credential_json FROM provider_credentials WHERE provider_id = $1", provider_id)
        if not row or not row["credential_json"]:
            return None
        return _resolve_credential(row["credential_json"], use_testnet)

    async def insert_position(self, *, ticker_id, strategy_id, side, entry_price, quantity, is_probation,
                              leverage: int = 1, margin_type: str = "isolated",
                              trade_mode: str = "paper", network: Optional[str] = None) -> str:
        async with self.pool.acquire() as conn:
            pid = await conn.fetchval(
                """
                INSERT INTO positions
                    (ticker_id, strategy_id, side, status, entry_price, current_price, quantity,
                     unrealized_pl, realized_pl, is_probation, leverage, margin_type, trade_mode, network)
                VALUES ($1, $2, $3, 'open', $4, $4, $5, 0, 0, $6, $7, $8, $9, $10)
                RETURNING id
                """,
                ticker_id, strategy_id, side, entry_price, quantity, is_probation,
                leverage, margin_type, trade_mode, network,
            )
        return str(pid)

    async def insert_order(self, *, position_id, provider_order_id, side, quantity, price) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (position_id, provider_order_id, side, quantity, price, order_type, status, filled_at)
                VALUES ($1, $2, $3, $4, $5, 'market', 'filled', now())
                """,
                position_id, provider_order_id, side, quantity, price,
            )

    async def get_position(self, position_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.id, p.ticker_id, p.side, p.status, p.entry_price, p.current_price, p.quantity,
                       t.provider_id, t.symbol, mt.name AS market_type
                FROM positions p LEFT JOIN tickers t ON t.id = p.ticker_id
                       LEFT JOIN market_types mt ON mt.id = t.market_type_id
                WHERE p.id = $1
                """,
                position_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "tickerId": str(row["ticker_id"]), "side": row["side"],
                "status": row["status"], "entryPrice": row["entry_price"], "currentPrice": row["current_price"],
                "quantity": row["quantity"], "providerId": str(row["provider_id"]) if row["provider_id"] else None,
                "symbol": row["symbol"], "marketType": row["market_type"]}

    async def close_position_row(self, *, position_id, exit_price, realized_pl) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions
                SET status = 'closed', exit_price = $2, current_price = $2, realized_pl = $3,
                    unrealized_pl = 0, closed_at = now()
                WHERE id = $1
                """,
                position_id, exit_price, realized_pl,
            )

    async def find_open_positions_by_ticker(self, ticker_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.id, p.side, p.entry_price, p.current_price, p.quantity, t.provider_id
                FROM positions p LEFT JOIN tickers t ON t.id = p.ticker_id
                WHERE p.ticker_id = $1 AND p.status = 'open'
                """,
                ticker_id,
            )
        return [{"id": str(r["id"]), "side": r["side"], "entryPrice": r["entry_price"],
                 "currentPrice": r["current_price"], "quantity": r["quantity"],
                 "providerId": str(r["provider_id"]) if r["provider_id"] else None} for r in rows]

    async def find_open_positions_by_provider(self, provider_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.id, p.entry_price, p.current_price
                FROM positions p LEFT JOIN tickers t ON t.id = p.ticker_id
                WHERE t.provider_id = $1 AND p.status = 'open'
                """,
                provider_id,
            )
        return [{"id": str(r["id"]), "entryPrice": r["entry_price"], "currentPrice": r["current_price"]} for r in rows]

    async def get_hard_caps(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT hard_stop_loss_pct, hard_take_profit_pct FROM risk_limits WHERE provider_id = $1", provider_id
            )
        if not row:
            return None
        return {"hardStopLossPct": row["hard_stop_loss_pct"], "hardTakeProfitPct": row["hard_take_profit_pct"]}

    async def update_position_mark(self, position_id: str, current_price: float, unrealized_pl: float) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE positions SET current_price = $2, unrealized_pl = $3 WHERE id = $1",
                position_id, current_price, unrealized_pl,
            )

    async def record_api_success(self, provider_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE providers SET api_failure_count = 0 WHERE id = $1 AND api_failure_count != 0", provider_id)

    async def record_api_failure(self, provider_id: str) -> None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT kill_switch_active, api_failure_count, name FROM providers WHERE id = $1", provider_id
            )
            if not row or row["kill_switch_active"]:
                return
            new_count = int(row["api_failure_count"]) + 1
            await conn.execute("UPDATE providers SET api_failure_count = $2 WHERE id = $1", provider_id, new_count)
            if new_count >= API_FAILURE_KILL_THRESHOLD:
                reason = f"{new_count} consecutive provider API failures (threshold {API_FAILURE_KILL_THRESHOLD})."
                await conn.execute(
                    "UPDATE providers SET kill_switch_active = true, kill_switch_reason = $2 WHERE id = $1",
                    provider_id, reason,
                )
                # NOTE(3c-3): also publish to Redis alerts:created for live dashboard delivery.
                await conn.execute(
                    "INSERT INTO alerts (severity, category, message, related_entity_type, related_entity_id) "
                    "VALUES ('critical', 'KILL_SWITCH_TRIGGERED', $2, 'Provider', $1)",
                    provider_id,
                    f"Kill switch triggered for provider {row['name']}: {reason}. All new orders blocked until manually cleared.",
                )
