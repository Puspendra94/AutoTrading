"""asyncpg-backed GeneratorStore — the production DB adapter for StrategyGenerator.

All SQL lives here so the orchestration in generator.py stays DB-agnostic and unit-testable.
Relies on the pool's jsonb codec (worker/db.py) so jsonb columns round-trip as Python objects.
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg

from ..execution.signals_pg_store import _INTERVAL_RE, _PG_UNIT, _rows_to_candles


def _ts_ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def _f(v: Any) -> Optional[float]:
    """Coerce a DB numeric (asyncpg hands these back as decimal.Decimal) to a plain float.
    Decimal is not JSON-serializable, and these values are dumped into LLM prompts."""
    return None if v is None else float(v)


class PgGeneratorStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool  # also read by StrategyGenerator for llm.log_cost

    async def get_ticker(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT t.id, t.symbol, t.interval, mt.name AS market_type_name
                FROM tickers t LEFT JOIN market_types mt ON mt.id = t.market_type_id
                WHERE t.id = $1
                """,
                ticker_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "symbol": row["symbol"], "interval": row["interval"],
                "market_type_name": row["market_type_name"]}

    async def has_unresolved_blocking_flags(self, ticker_id: str) -> bool:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM data_quality_flags "
                "WHERE ticker_id = $1 AND flag_type IN ('gap','spike') AND resolved_at IS NULL",
                ticker_id,
            )
        return int(n) > 0

    async def set_onboarding_stage(self, ticker_id: str, stage: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tickers SET onboarding_stage = $2 WHERE id = $1", ticker_id, stage)

    async def load_recent_candles(self, ticker_id: str, limit: int = 2000) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT close, timestamp FROM ohlcv_data WHERE ticker_id = $1 ORDER BY timestamp DESC LIMIT $2",
                ticker_id, limit,
            )
        return [{"close": float(r["close"]), "timestamp": _ts_ms(r["timestamp"])} for r in reversed(rows)]

    async def load_candles_for_interval(self, ticker_id: str, interval: str, limit: int) -> list[dict]:
        """Full OHLCV candles aggregated to `interval` (ASC, ms ts) for the DSL backtest — mirrors
        the backend's getCandlesForInterval time_bucket roll-up. The DSL evaluator needs high/low/
        volume (not just close), unlike the legacy close-only path."""
        if interval == "1m":
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT timestamp, open, high, low, close, volume FROM ohlcv_data "
                    "WHERE ticker_id = $1 ORDER BY timestamp DESC LIMIT $2",
                    ticker_id, limit,
                )
            return _rows_to_candles(list(reversed(rows)))
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

    async def get_latest_strategy_params(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parameters_json FROM strategies WHERE ticker_id = $1 ORDER BY version DESC LIMIT 1",
                ticker_id,
            )
        return row["parameters_json"] if row else None

    async def get_active_policy(self) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT min_sharpe, max_drawdown_pct, min_profit_factor, min_trade_count, max_parameter_count "
                "FROM strategy_evaluation_policy WHERE is_active = true LIMIT 1"
            )
        if row:
            return {
                "minSharpe": row["min_sharpe"], "maxDrawdownPct": row["max_drawdown_pct"],
                "minProfitFactor": row["min_profit_factor"], "minTradeCount": row["min_trade_count"],
                "maxParameterCount": row["max_parameter_count"],
            }
        # Matches the backend's in-memory default (entity defaults apply maxParameterCount=5).
        return {"minSharpe": 1.0, "maxDrawdownPct": 20.0, "minProfitFactor": 1.3,
                "minTradeCount": 100, "maxParameterCount": 5}

    async def count_strategies(self, ticker_id: str) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM strategies WHERE ticker_id = $1", ticker_id)
        return int(n)

    async def insert_strategy(self, *, ticker_id: str, version: int, parameters_json: dict, generated_by: str,
                              eval_interval: Optional[str] = None) -> str:
        async with self.pool.acquire() as conn:
            sid = await conn.fetchval(
                """
                INSERT INTO strategies (ticker_id, version, status, execution_mode, parameters_json,
                                        generated_by, eval_interval)
                VALUES ($1, $2, 'draft', 'mode_a_rules', $3, $4, $5)
                RETURNING id
                """,
                ticker_id, version, parameters_json, generated_by, eval_interval,
            )
        return str(sid)

    async def insert_backtest(self, *, strategy_id: str, ev: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO backtest_results
                    (strategy_id, sharpe, sortino, calmar, max_drawdown, drawdown_duration, profit_factor,
                     trade_count, total_return_pct, win_rate, monte_carlo_summary_json, regime_breakdown_json,
                     parameter_count, passed_evaluation_gate)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                """,
                strategy_id, ev["sharpe"], ev["sortino"], ev["calmar"], ev["maxDrawdown"], ev["drawdownDuration"],
                ev["profitFactor"], ev["tradeCount"], ev["totalReturnPct"], ev["winRate"],
                ev["monteCarloSummary"], ev["regimeBreakdown"], ev["parameterCount"], ev["passedEvaluationGate"],
            )

    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, version, parameters_json FROM strategies WHERE ticker_id = $1 AND status = 'live' LIMIT 1",
                ticker_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "version": row["version"], "parametersJson": row["parameters_json"]}

    async def get_backtest_metrics(self, strategy_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT sharpe, profit_factor, max_drawdown, passed_evaluation_gate "
                "FROM backtest_results WHERE strategy_id = $1",
                strategy_id,
            )
        if not row:
            return None
        # asyncpg maps numeric/decimal columns to decimal.Decimal, which json.dumps cannot encode —
        # these metrics get JSON-dumped into the lesson prompt. Coerce at the boundary so callers
        # always receive plain numbers.
        return {"sharpe": _f(row["sharpe"]), "profitFactor": _f(row["profit_factor"]),
                "maxDrawdown": _f(row["max_drawdown"]), "passedEvaluationGate": row["passed_evaluation_gate"]}

    async def get_latest_divergence(self, strategy_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, divergence_pct, flagged FROM live_vs_backtest_divergence "
                "WHERE strategy_id = $1 ORDER BY measured_at DESC LIMIT 1",
                strategy_id,
            )
        if not row:
            return None
        return {"id": str(row["id"]), "divergencePct": _f(row["divergence_pct"]), "flagged": row["flagged"]}

    async def retire_live(self, ticker_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE strategies SET status = 'retired' WHERE ticker_id = $1 AND status = 'live'", ticker_id)

    async def promote_strategy(self, strategy_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE strategies SET status = 'live' WHERE id = $1", strategy_id)

    async def set_ticker_active_ready(self, ticker_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE tickers SET status = 'active', onboarding_stage = 'ready' WHERE id = $1", ticker_id)

    async def retrieve_lessons(self, ticker_id: str, strategy_type: Optional[str], limit: int = 5) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, outcome, summary_text FROM ai_lessons_learned "
                "WHERE ticker_id = $1 ORDER BY created_at DESC LIMIT $2",
                ticker_id, limit,
            )
            ticker_matches = [{"id": str(r["id"]), "outcome": r["outcome"], "summaryText": r["summary_text"]} for r in rows]
            if len(ticker_matches) >= limit or not strategy_type:
                return ticker_matches

            remaining = limit - len(ticker_matches)
            seen = {l["id"] for l in ticker_matches}
            rows2 = await conn.fetch(
                "SELECT id, outcome, summary_text FROM ai_lessons_learned "
                "WHERE strategy_type = $1 AND ticker_id != $2 ORDER BY created_at DESC LIMIT $3",
                strategy_type, ticker_id, remaining + len(seen),
            )
        broader = [
            {"id": str(r["id"]), "outcome": r["outcome"], "summaryText": r["summary_text"]}
            for r in rows2 if str(r["id"]) not in seen
        ]
        return (ticker_matches + broader)[:limit]

    async def insert_lesson(self, **kw: Any) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ai_lessons_learned
                    (source_strategy_id, source_divergence_id, ticker_id, market_type, strategy_type,
                     regime_tags, outcome, summary_text)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                kw["source_strategy_id"], kw["source_divergence_id"], kw["ticker_id"], kw["market_type"],
                kw["strategy_type"], kw["regime_tags"], kw["outcome"], kw["summary_text"],
            )
