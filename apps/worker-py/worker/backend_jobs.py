"""Triggers the backend's scheduled-job endpoints.

The former Node worker's six cron jobs now live as guarded /internal/jobs/* endpoints in the
API process (they reuse the exact same services — no duplicated trading logic). This module
is the scheduler side: it POSTs to those endpoints with the shared INTERNAL_API_KEY, so the
Python worker is the single 24/7 trigger for all of them.
"""
from __future__ import annotations

import logging

import httpx

from .config import config

log = logging.getLogger("worker.jobs")


async def trigger(job_path: str) -> dict | None:
    """POST /internal/jobs/<job_path> on the backend. Returns the JSON result, or None on
    failure (logged, never raised — a failed job trigger must not crash the scheduler)."""
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
