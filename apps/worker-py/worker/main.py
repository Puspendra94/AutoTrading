"""Entry point for the standalone Python Data + AI worker (runs 24/7 in its own container).

It is now the single background scheduler for the whole platform — the NestJS worker has
been retired. Responsibilities:

  * DATA (native here): full-history archive backfill from data.binance.vision + a daily
    gap-fill, plus a gap-fill on every startup.
  * ALL former Node-worker jobs (balance-sync, reconciliation, allocation-rebalance,
    alert-digest, strategy re-evaluation/regeneration, data-quality): scheduled here and
    executed by the backend's guarded /internal/jobs/* endpoints (worker/backend_jobs.py),
    at the same cadences the old @Cron/@Interval decorators used. The trading/AI logic
    stays in the single, proven backend implementation — this process owns the *when*.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import backend_jobs
from .binance_archive import backfill_all
from .config import config
from .db import close_pool

logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("worker.main")


async def _daily_backfill() -> None:
    log.info("Scheduled archive gap-fill starting…")
    results = await backfill_all()
    log.info("Scheduled archive gap-fill done: %s", results)


# Seconds to wait between startup-backfill attempts. Sized for the cold-boot race: the backend
# owns the migrations and compose cannot express a dependency across the two stacks, so on a
# fresh host this process can reach the database before `tickers` exists.
_BACKFILL_RETRY_DELAYS = (5, 15, 30, 60, 120)


async def _startup_backfill() -> None:
    """Gap-fill on startup, retrying the cold-boot race before giving up.

    This used to log the failure and carry on. That was the worst of both worlds: the process
    stayed up looking healthy while the history was never fetched, and because it never exited
    non-zero, `restart: unless-stopped` never retried it either — so a single unlucky boot left
    the worker permanently running on an empty candle table. Exhausting the retries now raises,
    which both surfaces the problem and lets the restart policy have another go.
    """
    log.info("Running startup archive gap-fill…")
    for attempt, delay in enumerate((*_BACKFILL_RETRY_DELAYS, None), start=1):
        try:
            results = await backfill_all()
            log.info("Startup backfill complete: %s", results)
            return
        except Exception:  # noqa: BLE001
            if delay is None:
                log.exception("Startup backfill failed after %d attempts — no history was fetched", attempt)
                raise
            log.warning(
                "Startup backfill attempt %d failed; retrying in %ds", attempt, delay, exc_info=True
            )
            await asyncio.sleep(delay)


async def main() -> None:
    log.info("Python Data+AI worker starting (sole background scheduler).")

    # Resolve the trading brain before anything is scheduled — an unknown value must stop the
    # process, not silently fall back to 'strategy' and re-arm an engine believed to be off.
    from .execution.factory import KNOWN_BRAINS, PATTERN_BRAIN

    brain = config.trading_brain
    if brain not in KNOWN_BRAINS:
        raise SystemExit(
            f"Unknown TRADING_BRAIN '{brain}' — must be one of: {', '.join(KNOWN_BRAINS)}."
        )
    strategy_brain_active = brain != PATTERN_BRAIN
    log.info("TRADING_BRAIN=%s", brain)

    # Backfill missing data on every startup (cheap — resumes from the last stored month).
    if config.backfill_on_start:
        await _startup_backfill()

    scheduler = AsyncIOScheduler(timezone="UTC")

    # --- Data ingestion (native) ---
    scheduler.add_job(_daily_backfill, CronTrigger(hour=0, minute=30), id="archive_backfill", max_instances=1)

    # --- Former Node-worker jobs, same cadences, executed by the backend ---
    scheduler.add_job(
        backend_jobs.balance_sync,
        IntervalTrigger(minutes=config.balance_sync_interval_minutes),
        id="balance_sync", max_instances=1,
    )
    scheduler.add_job(backend_jobs.reconciliation, CronTrigger(minute=0), id="reconciliation", max_instances=1)  # hourly
    scheduler.add_job(backend_jobs.data_quality, CronTrigger(hour="*/6", minute=15), id="data_quality", max_instances=1)
    scheduler.add_job(backend_jobs.alert_digest, CronTrigger(hour="*/4", minute=5), id="alert_digest", max_instances=1)
    scheduler.add_job(backend_jobs.allocation_rebalance, CronTrigger(hour=2, minute=0), id="allocation_rebalance", max_instances=1)

    # Strategy re-evaluation can RETIRE a live strategy and trigger regeneration, which ends in a
    # newly promoted 'live' strategy. Under the pattern brain that would silently re-arm the engine
    # we just switched off, so the job is not scheduled at all.
    if strategy_brain_active:
        scheduler.add_job(backend_jobs.strategy_reevaluation, CronTrigger(hour=1, minute=0), id="strategy_reevaluation", max_instances=1)

    # --- Strategy Supervisor (Phase 3a) — continuous deterministic guardrail monitor. When
    # enabled it is the responsive replacement for the daily strategy_reevaluation trigger.
    if config.supervisor_enabled and strategy_brain_active:
        from . import supervisor

        scheduler.add_job(
            supervisor.run_supervisor_once,
            IntervalTrigger(minutes=config.supervisor_interval_minutes),
            id="strategy_supervisor", max_instances=1,
        )
        log.info("Strategy Supervisor enabled (every %d min).", config.supervisor_interval_minutes)

    scheduler.start()
    log.info("Scheduler started. Jobs: %s", [j.id for j in scheduler.get_jobs()])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover — e.g. Windows
            pass

    # --- Live ingestion (native, Phase 1) — worker owns the Binance kline stream and is
    # the writer of record for live candles. Gated so it doesn't double-ingest while the
    # backend still streams in-process (see LIVE_STREAM_ENABLED / MIGRATION.md).
    live_task: asyncio.Task | None = None
    if config.live_stream_enabled:
        from .live_stream import run_live_stream

        live_task = asyncio.create_task(run_live_stream(stop))
        log.info("Live ingestion pipeline started (worker owns the kline stream).")
    else:
        log.info("Live ingestion disabled (LIVE_STREAM_ENABLED=false); backend still streams.")

    # Consolidation Phase A/B: when the worker owns the strategy engine, consume generation jobs
    # AND compute chart-signal markers on request (so the backend needs no DSL interpreter).
    # Both consumers belong to the strategy brain. signals_consumer in particular REPLAYS the live
    # strategy and WRITES rows into strategy_signals on every chart request, so leaving it running
    # under the pattern brain would keep repainting the chart with the old engine's markers.
    generate_task: asyncio.Task | None = None
    signals_task: asyncio.Task | None = None
    if config.generation_source == "worker" and strategy_brain_active:
        from .strategy.generate_consumer import run_generate_consumer
        from .strategy.signals_consumer import run_signals_consumer

        generate_task = asyncio.create_task(run_generate_consumer(stop))
        signals_task = asyncio.create_task(run_signals_consumer(stop))
        log.info("Strategy engine owned by worker (consuming strategy:generate + signals:request).")

    await stop.wait()

    log.info("Shutting down…")
    scheduler.shutdown(wait=False)
    if live_task is not None:
        await live_task
    if generate_task is not None:
        await generate_task
    if signals_task is not None:
        await signals_task
    from .redis_bus import close_redis

    await close_redis()
    await close_pool()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
