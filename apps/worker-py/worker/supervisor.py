"""Strategy Supervisor (Phase 3a, see MIGRATION.md) — the deterministic, no-AI health
monitor for live strategies.

On a fixed cadence it reads each LIVE strategy's own closed trades and computes a handful of
guardrail metrics — live-vs-backtest profit-factor divergence, consecutive losses, realized
drawdown, win-rate decay. If any threshold trips, it publishes `strategy:regenerate`; the
backend consumer then runs the existing (proven) generateStrategyForTicker path. So the
worker owns *when* to regenerate (cheaply, no AI, no LLM spend), while the one audited
generation implementation stays in the backend until it is ported in Phase 3b.

All the decision math is pure functions (below) so it can be unit-tested without a DB.
"""
from __future__ import annotations

import json
import logging

from .config import config
from .db import get_pool
from .redis_bus import STRATEGY_REGENERATE_CHANNEL, get_redis

log = logging.getLogger("worker.supervisor")

# Strategy ids we've already asked to regenerate — dedupe so we publish once per live
# strategy, not every cycle. After regeneration the backend promotes a *new* strategy id,
# which is evaluated fresh.
_flagged: set[str] = set()


# --------------------------------------------------------------------------------------
# Pure guardrail math (no I/O) — mirrors the backend's divergence formula and adds the
# real-time guardrails. Kept side-effect-free for straightforward unit testing.
# --------------------------------------------------------------------------------------

def profit_factor(realized_pls: list[float]) -> float:
    """Gross profit / gross loss. Matches strategy-engine.service.ts: when there are no
    losses, 3.0 if any profit else 0."""
    gross_profit = sum(p for p in realized_pls if p > 0)
    gross_loss = abs(sum(p for p in realized_pls if p <= 0))
    if gross_loss > 0:
        return gross_profit / gross_loss
    return 3.0 if gross_profit > 0 else 0.0


def divergence_pct(live: float, expected: float) -> float:
    """Percentage divergence of a live metric from its backtest expectation."""
    if expected != 0:
        return abs(live - expected) / abs(expected) * 100
    return 100.0


def consecutive_losses(realized_pls: list[float]) -> int:
    """Number of losing trades at the tail of the sequence (most recent first)."""
    count = 0
    for p in reversed(realized_pls):
        if p <= 0:
            count += 1
        else:
            break
    return count


def max_drawdown_pct(realized_pls: list[float], notional_base: float = 0.0) -> float:
    """Largest peak-to-trough drop of the equity curve (notional_base + cumulative realized
    PnL), as a % of the running peak. Measuring against a notional account base — as
    StrategyPerformance does — keeps a small early dip from reading as a huge % drawdown.
    Pass 0.0 for the raw cumulative-PnL drawdown."""
    equity = notional_base
    peak = notional_base
    max_dd = 0.0
    for p in realized_pls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            dd = (peak - equity) / peak * 100
            max_dd = max(max_dd, dd)
    return max_dd


def win_rate_pct(realized_pls: list[float]) -> float:
    if not realized_pls:
        return 0.0
    wins = sum(1 for p in realized_pls if p > 0)
    return wins / len(realized_pls) * 100


def evaluate(realized_pls: list[float], backtest_profit_factor: float) -> str | None:
    """Return a human-readable trip reason if any guardrail is breached, else None.
    Order matters only for which reason is reported first."""
    if len(realized_pls) < config.supervisor_min_closed_trades:
        return None  # not enough live trades to judge yet (matches backend's >=5 gate)

    live_pf = profit_factor(realized_pls)
    div = divergence_pct(live_pf, backtest_profit_factor)
    if div >= config.supervisor_divergence_pct:
        return (f"live-vs-backtest divergence {div:.1f}% exceeded {config.supervisor_divergence_pct:.0f}% "
                f"(live PF {live_pf:.2f} vs backtest {backtest_profit_factor:.2f})")

    losses = consecutive_losses(realized_pls)
    if losses >= config.supervisor_max_consecutive_losses:
        return f"{losses} consecutive losing trades reached limit of {config.supervisor_max_consecutive_losses}"

    dd = max_drawdown_pct(realized_pls, config.supervisor_notional_base)
    if dd >= config.supervisor_max_drawdown_pct:
        return f"realized drawdown {dd:.1f}% exceeded {config.supervisor_max_drawdown_pct:.0f}%"

    wr = win_rate_pct(realized_pls)
    if wr < config.supervisor_min_win_rate_pct:
        return f"win rate {wr:.1f}% fell below floor {config.supervisor_min_win_rate_pct:.0f}%"

    return None


# --------------------------------------------------------------------------------------
# I/O: load live strategies + their trades, evaluate, publish regeneration triggers.
# --------------------------------------------------------------------------------------

async def _load_live_strategies() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.ticker_id, s.version, b.profit_factor
            FROM strategies s
            JOIN backtest_results b ON b.strategy_id = s.id
            WHERE s.status = 'live'
            """
        )
    return [dict(r) for r in rows]


async def _load_closed_realized_pls(strategy_id: str) -> list[float]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT realized_pl FROM positions
            WHERE strategy_id = $1 AND status = 'closed'
            ORDER BY closed_at ASC
            """,
            strategy_id,
        )
    return [float(r["realized_pl"]) for r in rows]


async def run_supervisor_once() -> dict:
    """One evaluation sweep over all live strategies. Safe to call on a schedule."""
    strategies = await _load_live_strategies()
    checked = 0
    triggered = 0
    for s in strategies:
        checked += 1
        if s["id"] in _flagged:
            continue  # already asked to regenerate this exact strategy
        realized_pls = await _load_closed_realized_pls(s["id"])
        reason = evaluate(realized_pls, float(s["profit_factor"]))
        if reason is None:
            continue

        _flagged.add(s["id"])
        try:
            if config.supervisor_generate_inline:
                await _regenerate_inline(s["ticker_id"], reason)
            else:
                payload = {
                    "tickerId": s["ticker_id"],
                    "strategyId": s["id"],
                    "reason": reason,
                    "triggeredBy": "supervisor",
                }
                await get_redis().publish(STRATEGY_REGENERATE_CHANNEL, json.dumps(payload))
            triggered += 1
            log.warning("Strategy %s (ticker %s) tripped a guardrail — %s. Regeneration %s.",
                        s["id"], s["ticker_id"], reason,
                        "run in-process" if config.supervisor_generate_inline else "requested from backend")
        except Exception as err:  # noqa: BLE001 — a failure must not kill the sweep
            _flagged.discard(s["id"])  # allow a retry next cycle
            log.warning("Regeneration trigger failed for strategy %s: %s", s["id"], err)

    return {"checked": checked, "triggered": triggered}


async def _regenerate_inline(ticker_id: str, reason: str) -> None:
    """Run the ported generation pipeline in the worker (Phase 3b-3), instead of asking the
    backend to. Imported lazily so the supervisor's deterministic path has no LLM deps."""
    from .llm.service import LlmService
    from .strategy.generator import StrategyGenerator
    from .strategy.pg_store import PgGeneratorStore

    pool = await get_pool()
    generator = StrategyGenerator(PgGeneratorStore(pool), LlmService())
    result = await generator.generate(ticker_id, reason)
    log.info("In-process regeneration for ticker %s: saved=%s attempts=%s",
             ticker_id, result.get("saved"), result.get("attempts"))
