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


async def main() -> None:
    log.info("Python Data+AI worker starting (sole background scheduler).")

    # Backfill missing data on every startup (cheap — resumes from the last stored month).
    if config.backfill_on_start:
        log.info("Running startup archive gap-fill…")
        try:
            results = await backfill_all()
            log.info("Startup backfill complete: %s", results)
        except Exception:  # noqa: BLE001
            log.exception("Startup backfill failed")

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
    scheduler.add_job(backend_jobs.strategy_reevaluation, CronTrigger(hour=1, minute=0), id="strategy_reevaluation", max_instances=1)
    scheduler.add_job(backend_jobs.allocation_rebalance, CronTrigger(hour=2, minute=0), id="allocation_rebalance", max_instances=1)

    # --- Strategy Supervisor (Phase 3a) — continuous deterministic guardrail monitor. When
    # enabled it is the responsive replacement for the daily strategy_reevaluation trigger.
    if config.supervisor_enabled:
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

    # Consolidation Phase A: when the worker owns generation, consume strategy:generate jobs.
    generate_task: asyncio.Task | None = None
    if config.generation_source == "worker":
        from .strategy.generate_consumer import run_generate_consumer

        generate_task = asyncio.create_task(run_generate_consumer(stop))
        log.info("Strategy generation owned by worker (consuming strategy:generate).")

    await stop.wait()

    log.info("Shutting down…")
    scheduler.shutdown(wait=False)
    if live_task is not None:
        await live_task
    if generate_task is not None:
        await generate_task
    from .redis_bus import close_redis

    await close_redis()
    await close_pool()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
