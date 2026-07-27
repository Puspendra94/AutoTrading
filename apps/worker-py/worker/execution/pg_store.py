"""asyncpg-backed RiskGateStore — DB adapter for the risk gate (Phase 3c-1).

Read-only plus the daily-loss breach side-effects (create tracker, mark breached, alert).
`flatten_all_positions` is intentionally deferred to Phase 3c-2 (it places real close orders);
until then it logs and no-ops. The gate is not yet wired to any caller, so nothing here runs
in the live path until 3c-2.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import asyncpg

log = logging.getLogger("worker.execution.riskgate")


def _as_date(value) -> date:
    """Coerce a 'YYYY-MM-DD' string (what the risk gate passes) to a datetime.date so asyncpg can
    bind it to a `date` column — asyncpg's date codec calls .toordinal() and rejects a raw str."""
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


class PgRiskGateStore:
    def __init__(self, pool: asyncpg.Pool, flatten_fn=None) -> None:
        self.pool = pool
        # Set by the live-execution factory (3c-2/3c-3) to ExecutionService.flatten so a
        # daily-loss breach actually flattens positions. Until wired, flatten is a loud no-op.
        self._flatten_fn = flatten_fn

    async def get_ticker(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, provider_id FROM tickers WHERE id = $1", ticker_id)
        return {"id": str(row["id"]), "providerId": str(row["provider_id"])} if row else None

    async def get_provider(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, kill_switch_active, kill_switch_reason, trading_enabled FROM providers WHERE id = $1",
                provider_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "killSwitchActive": row["kill_switch_active"],
                "killSwitchReason": row["kill_switch_reason"], "tradingEnabled": row["trading_enabled"]}

    async def get_schedule(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT active_days, active_start_time, active_end_time, timezone FROM provider_schedule WHERE provider_id = $1",
                provider_id,
            )
        if not row:
            return None
        # active_days is a TypeORM simple-array (comma-joined text).
        days = row["active_days"]
        return {"activeDays": days.split(",") if isinstance(days, str) else list(days),
                "activeStartTime": row["active_start_time"], "activeEndTime": row["active_end_time"],
                "timezone": row["timezone"]}

    async def get_risk_limit(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT daily_loss_limit_pct, max_concurrent_positions_per_ticker, probation_size_pct, "
                "probation_trades_count FROM risk_limits WHERE provider_id = $1",
                provider_id,
            )
        if not row:
            return None
        return {"dailyLossLimitPct": row["daily_loss_limit_pct"],
                "maxConcurrentPositionsPerTicker": row["max_concurrent_positions_per_ticker"],
                "probationSizePct": row["probation_size_pct"], "probationTradesCount": row["probation_trades_count"]}

    async def get_today_tracker(self, provider_id: str, today: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT realized_pl, unrealized_pl, limit_breached FROM daily_loss_tracking "
                "WHERE provider_id = $1 AND tracking_date = $2",
                provider_id, _as_date(today),
            )
        if not row:
            return None
        return {"realizedPl": row["realized_pl"], "unrealizedPl": row["unrealized_pl"], "limitBreached": row["limit_breached"]}

    async def get_latest_balance(self, provider_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tradable_balance FROM provider_balance_snapshots WHERE provider_id = $1 ORDER BY synced_at DESC LIMIT 1",
                provider_id,
            )
        return {"tradableBalance": row["tradable_balance"]} if row else None

    async def create_today_tracker(self, provider_id: str, today: str, cum_base: float) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO daily_loss_tracking "
                "(provider_id, tracking_date, capital_under_management_base, realized_pl, unrealized_pl, limit_breached) "
                "VALUES ($1, $2, $3, 0, 0, false)",
                provider_id, _as_date(today), cum_base,
            )

    async def mark_breached(self, provider_id: str, today: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE daily_loss_tracking SET limit_breached = true, breached_at = now() "
                "WHERE provider_id = $1 AND tracking_date = $2",
                provider_id, _as_date(today),
            )

    async def create_alert(self, severity: str, category: str, message: str, related_type: str, related_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO alerts (severity, category, message, related_entity_type, related_entity_id) "
                "VALUES ($1, $2, $3, $4, $5)",
                severity, category, message, related_type, related_id,
            )

    async def flatten_all_positions(self, provider_id: str, reason: str) -> None:
        if self._flatten_fn is not None:
            await self._flatten_fn(provider_id, reason)
            return
        # No execution wired in — surface loudly so a breach is never silently un-flattened.
        log.error("flatten_all_positions requested for provider %s (%s) but no execution engine "
                  "is wired to this gate; positions NOT flattened.", provider_id, reason)

    async def count_open_positions(self, ticker_id: str) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM positions WHERE ticker_id = $1 AND status = 'open'", ticker_id)
        return int(n)

    async def get_latest_allocation(self, provider_id: str, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT allocated_capital FROM allocation_snapshots WHERE provider_id = $1 AND ticker_id = $2 "
                "ORDER BY computed_at DESC LIMIT 1",
                provider_id, ticker_id,
            )
        return {"allocatedCapital": row["allocated_capital"]} if row else None

    async def count_active_tickers(self, provider_id: str) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM tickers WHERE provider_id = $1 AND status = 'active'", provider_id)
        return int(n)

    async def get_strategy(self, strategy_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT status FROM strategies WHERE id = $1", strategy_id)
        return {"status": row["status"]} if row else None

    async def count_positions_for_strategy(self, strategy_id: str) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM positions WHERE strategy_id = $1", strategy_id)
        return int(n)
