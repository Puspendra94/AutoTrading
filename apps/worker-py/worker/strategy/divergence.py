"""Daily live-vs-backtest divergence check (consolidation Phase B) — port of
strategy-engine.service.ts::runDivergenceCheckForAllLiveStrategies.

Pure DB + arithmetic (no DSL): for each live strategy with enough closed live trades, compare the
realized profit factor to the backtest's; record the divergence; and if it exceeds the threshold,
flag it and trigger a regeneration (via the worker's own strategy:generate consumer). Runs in the
worker so the backend needs no strategy engine.
"""
from __future__ import annotations

import json
import logging
import uuid

from ..db import get_pool
from ..redis_bus import STRATEGY_GENERATE_CHANNEL, get_redis

log = logging.getLogger("worker.strategy.divergence")

DIVERGENCE_FLAG_THRESHOLD_PCT = 30.0  # spec 7.5 — significant divergence triggers regeneration


async def run_divergence_check() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        live = await conn.fetch("SELECT id, ticker_id FROM strategies WHERE status = 'live'")
    checked = 0
    flagged = 0
    regenerated = 0

    for s in live:
        checked += 1
        strategy_id = str(s["id"])
        ticker_id = str(s["ticker_id"])
        async with pool.acquire() as conn:
            expected_row = await conn.fetchrow(
                "SELECT profit_factor FROM backtest_results WHERE strategy_id = $1", strategy_id)
            if not expected_row:
                continue
            realized = await conn.fetch(
                "SELECT realized_pl FROM positions WHERE strategy_id = $1 AND status = 'closed'", strategy_id)
        if len(realized) < 5:
            continue  # not enough live trades yet to judge

        pls = [float(r["realized_pl"]) for r in realized]
        gross_profit = sum(p for p in pls if p > 0)
        gross_loss = abs(sum(p for p in pls if p <= 0))
        live_pf = gross_profit / gross_loss if gross_loss > 0 else (3.0 if gross_profit > 0 else 0.0)

        expected = float(expected_row["profit_factor"])
        divergence_pct = (abs(live_pf - expected) / abs(expected)) * 100 if expected != 0 else 100.0
        is_flagged = divergence_pct >= DIVERGENCE_FLAG_THRESHOLD_PCT

        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO live_vs_backtest_divergence "
                "(strategy_id, backtest_expected_metric, live_actual_metric, divergence_pct, flagged) "
                "VALUES ($1, $2, $3, $4, $5)",
                strategy_id, expected, round(live_pf, 4), round(divergence_pct, 2), is_flagged,
            )

        if is_flagged:
            flagged += 1
            reason = (f"live-vs-backtest divergence {divergence_pct:.1f}% exceeded "
                      f"{DIVERGENCE_FLAG_THRESHOLD_PCT:.0f}% threshold")
            log.warning("Strategy %s (ticker %s) flagged: live PF %.2f vs backtest %.2f (%.1f%%). Triggering regeneration.",
                        strategy_id, ticker_id, live_pf, expected, divergence_pct)
            await get_redis().publish(STRATEGY_GENERATE_CHANNEL,
                                      json.dumps({"requestId": str(uuid.uuid4()), "tickerId": ticker_id, "reason": reason}))
            regenerated += 1

    log.info("Divergence check: %d live strategies checked, %d flagged, %d regenerations triggered.",
             checked, flagged, regenerated)
    return {"checked": checked, "flagged": flagged, "regenerated": regenerated}
