# Python Data + AI Worker

A standalone Python 3.12 service that runs 24/7 and is the platform's **sole background
scheduler**. The NestJS worker has been retired — this process now owns *when* every
background job runs. It shares the same Postgres/TimescaleDB (`Algo_Trading` schema) and
Redis as the backend.

## What it does

**Data ingestion (native, in this service):**
- **Full-history archive backfill** (`worker/binance_archive.py`) — pulls BTCUSDT **1m**
  klines since 2017 from `data.binance.vision` (monthly + current-month daily zips),
  parses the CSVs, and bulk-loads them into the shared `ohlcv_data` hypertable. Idempotent
  and resumable; safe to run alongside the API's live stream. We store only the 1m base —
  the minimum interval — as the single source of truth.
- **DB-level interval roll-ups** (`worker/candles.py`) — any higher interval (5m/1h/1d/…)
  is derived from the 1m base with TimescaleDB `time_bucket` (first/max/min/last/sum), so
  switching interval is cheap and needs no duplicate ingestion.
- **Backfill on every restart** — `main.py` runs a gap-fill on startup (resumes from the
  last stored month), plus a daily cron (00:30 UTC).

**All former Node-worker jobs (`worker/backend_jobs.py`):** the six cron/interval jobs
that used to live in `apps/backend/src/jobs/` are scheduled here and executed by the
backend's guarded `POST /internal/jobs/*` endpoints, at the same cadences. The trading/AI
logic (strategy re-evaluation & regeneration, reconciliation, allocation, balance sync,
alert digest, data-quality sweep) stays in the single, proven backend implementation —
this process just triggers it. This keeps one source of truth for trading logic and leaves
the live Mode-B LLM path untouched in the API.

| Job | Cadence | Runs via |
| --- | --- | --- |
| Archive backfill / gap-fill | startup + daily 00:30 | native (this service) |
| Balance sync | every `BALANCE_SYNC_INTERVAL_MINUTES` (5) | `POST /internal/jobs/balance-sync` |
| Data-quality sweep | every 6h | `POST /internal/jobs/data-quality` |
| Alert digest | every 4h | `POST /internal/jobs/alert-digest` |
| Reconciliation | hourly | `POST /internal/jobs/reconciliation` |
| Strategy re-evaluation (+ regen) | daily 01:00 | `POST /internal/jobs/strategy-reevaluation` |
| Allocation rebalance | daily 02:00 | `POST /internal/jobs/allocation-rebalance` |

The `/internal/jobs/*` endpoints require the shared `INTERNAL_API_KEY` (`x-internal-key`
header) and are never exposed to browsers.

## Running

```bash
cd apps/worker-py
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install asyncpg httpx apscheduler python-dotenv
cp .env.example .env        # point at your Postgres/Redis + backend URL + INTERNAL_API_KEY

# One-shot initial import (long-running for full 1m history):
python -m worker.backfill                 # full 1m history
python -m worker.backfill 1m 2024-11      # override start month (quick smoke test)

# Long-running scheduler (what the container runs):
python -m worker.main
```

In Docker it's the `worker-py` service in the project-root `docker-compose.yml`, which sets
`BACKEND_URL=http://backend:3009` and `INTERNAL_API_KEY` so it can trigger the backend jobs
over the compose network.
