"""Triggers the backend's scheduled jobs.

The former Node worker's six cron jobs live as InternalJobsService bodies in the API process
(they reuse the exact same services — no duplicated trading logic). This module is the
scheduler side. Two dispatch modes (JOB_DISPATCH, see config):

  * http  — POST the guarded /internal/jobs/* endpoint synchronously (legacy; the worker
            depends on the backend being reachable at trigger time).
  * redis — publish the job name to the jobs:trigger channel; the backend consumes it
            asynchronously. The worker no longer blocks on, or fails because of, the
            backend, so the two processes deploy and restart independently (Phase 2).

Either way a failure is logged, never raised — a bad trigger must not crash the scheduler.
"""
from __future__ import annotations

import json
import logging

import httpx

from .config import config
from .redis_bus import JOBS_TRIGGER_CHANNEL, get_redis

log = logging.getLogger("worker.jobs")


async def trigger(job_path: str) -> dict | None:
    """Dispatch a scheduled job to the backend via the configured mode."""
    if config.job_dispatch == "redis":
        return await _trigger_redis(job_path)
    return await _trigger_http(job_path)


async def _trigger_redis(job_name: str) -> dict | None:
    """Publish the job name to Redis; the backend's consumer runs it. Fire-and-forget: no
    result is returned (the backend logs execution), and it does not require the backend to
    be up right now — a missed periodic tick simply runs on the next cadence."""
    try:
        await get_redis().publish(JOBS_TRIGGER_CHANNEL, json.dumps({"job": job_name}))
        log.info("Published job trigger '%s' to %s.", job_name, JOBS_TRIGGER_CHANNEL)
        return {"published": job_name}
    except Exception as err:  # noqa: BLE001 — never let a dispatch failure kill the scheduler
        log.warning("Failed to publish job trigger '%s': %s", job_name, err)
        return None


async def _trigger_http(job_path: str) -> dict | None:
    """POST /internal/jobs/<job_path> on the backend. Returns the JSON result, or None on
    failure (logged, never raised)."""
    if not config.internal_api_key:
        log.error("INTERNAL_API_KEY not set — cannot trigger backend job '%s'.", job_path)
        return None
    url = f"{config.backend_url}/internal/jobs/{job_path}"
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(url, headers={"x-internal-key": config.internal_api_key})
            resp.raise_for_status()
            result = resp.json()
            log.info("Triggered backend job '%s': %s", job_path, result)
            return result
    except Exception as err:  # noqa: BLE001 — never let a job failure kill the scheduler
        log.warning("Backend job '%s' failed: %s", job_path, err)
        return None


# Convenience wrappers, one per former cron job.
async def balance_sync():
    return await trigger("balance-sync")


async def reconciliation():
    return await trigger("reconciliation")


async def allocation_rebalance():
    return await trigger("allocation-rebalance")


async def alert_digest():
    return await trigger("alert-digest")


async def strategy_reevaluation():
    return await trigger("strategy-reevaluation")


async def data_quality():
    return await trigger("data-quality")
