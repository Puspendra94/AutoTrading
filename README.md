# AI-Based Algorithmic Trading Platform

An AI-assisted, multi-provider algorithmic trading platform. Large-language-model
strategies are generated and evaluated against real market data, executed through
exchange connectors, and continuously governed by hard risk guardrails. The system is
built as a small monorepo of three cooperating services backed by TimescaleDB and Redis.

> **Real data only.** The platform trades and backtests against real Binance market
> data — there is no synthetic/mock feed. Live trading is gated behind per-provider
> trading modes, risk limits, and a kill switch.

---

## Architecture

The platform is split into three long-running services plus shared infrastructure:

```
┌──────────────┐        HTTP /api, WS /socket.io       ┌───────────────────────┐
│   frontend   │  ───────────────────────────────────▶ │       backend         │
│  (AstroJS)   │ ◀───────────────────────────────────  │  (NestJS / TypeORM)   │
└──────────────┘         live ticks, alerts            └───────────┬───────────┘
                                                                    │
                                          /internal/jobs/*  ▲       │ TypeORM / ioredis
                                    (scheduled, API-key gated)│      ▼
┌──────────────┐   full-history backfill + job triggers  ┌───────────────────────┐
│  worker-py   │ ──────────────────────────────────────▶ │  TimescaleDB + Redis  │
│ (APScheduler)│                                          └───────────────────────┘
└──────────────┘
```

- **`backend` (NestJS)** — the single source of trading truth. Owns the domain logic:
  auth, providers/connectors, market data, LLM strategy generation, risk & execution,
  capital allocation, reconciliation, notifications, and the WebSocket gateway. Runs
  TypeORM migrations automatically on startup.
- **`frontend` (AstroJS)** — the operator dashboard. Server-rendered pages with a
  live TradingView-style chart (`lightweight-charts`) and a Socket.IO client for live
  ticks and alerts. In production it serves via a small Node process that proxies `/api`
  and `/socket.io` to the backend.
- **`worker-py` (Python / APScheduler)** — the sole background scheduler. Natively backfills
  full price history from `data.binance.vision`, and triggers every other periodic job by
  calling the backend's guarded `/internal/jobs/*` endpoints on the same cadences the old
  in-process cron jobs used. It owns the *when*; the backend owns the *how*.
- **Infrastructure** — TimescaleDB (Postgres 16 hypertables for OHLCV time-series) and
  Redis (pub/sub for live alerts, caching, and job coordination).

---

## Project Structure

```
├── apps/
│   ├── backend/          # NestJS API + WebSocket gateway + TypeORM migrations + trading logic
│   │   └── src/
│   │       ├── modules/
│   │       │   ├── auth/            # signup, login, forgot/reset password (JWT + bcrypt)
│   │       │   ├── provider/        # exchange connectors, credentials, trading mode, kill switch
│   │       │   ├── market-data/     # tickers, OHLCV candles, backfill triggers
│   │       │   ├── strategy/        # LLM strategy generation, evaluation, performance
│   │       │   ├── risk-execution/  # order placement, positions, risk guardrails
│   │       │   ├── allocation/      # capital allocation across strategies/providers
│   │       │   ├── reconciliation/  # broker-vs-ledger reconciliation reports
│   │       │   ├── notification/    # alerts feed
│   │       │   ├── llm/             # LLM provider abstraction + cost logging
│   │       │   └── internal-jobs/   # API-key-guarded endpoints triggered by worker-py
│   │       ├── entities/           # TypeORM entities (see Data Model below)
│   │       ├── migrations/         # ordered schema migrations (run automatically)
│   │       └── websockets/         # Socket.IO gateway + Redis→WS alert bridge
│   ├── frontend/         # AstroJS dashboard (login, index, strategies, profile, reset)
│   └── worker-py/        # Python data + job-scheduling worker
├── infra/
│   └── docker-compose.yml  # TimescaleDB (5433), Redis (55000), Redis Commander (8081)
├── docker-compose.yml    # Project-level stack: backend + frontend + worker-py
├── .env.example          # Environment template
├── DESIGN.md             # Product/UI design reference
├── PROGRESS.md           # Build progress log
└── CONVENTIONS.md        # Coding conventions
```

---

## Tech Stack

| Layer          | Technology                                                              |
| -------------- | ----------------------------------------------------------------------- |
| Backend        | Node.js, NestJS 10, TypeScript 5, TypeORM 0.3                            |
| Frontend       | AstroJS 4, Tailwind CSS 4, `lightweight-charts`, Socket.IO client       |
| Data worker    | Python, APScheduler, asyncpg, httpx                                      |
| Database       | PostgreSQL 16 + TimescaleDB (hypertables for OHLCV)                      |
| Cache / pub-sub| Redis 7 (ioredis)                                                        |
| Real-time      | Socket.IO (WebSocket gateway)                                           |
| Auth           | JWT (`@nestjs/jwt`, passport-jwt) + bcrypt password hashing             |
| LLM            | LangChain — Anthropic (direct API) or AWS Bedrock; DeepSeek supported   |
| Exchange       | `@binance/connector` (Binance Spot / Futures)                           |

---

## Prerequisites

- **Node.js** 20+ and npm
- **Python** 3.12+ (for `worker-py`)
- **Docker** + Docker Compose (for infrastructure and/or the full stack)
- An **Anthropic API key** (or AWS Bedrock credentials) for LLM strategy generation
- **Binance API credentials** (added per-provider through the UI, not via env)

---

## Quick Start (local development)

Run each service on its own port against Dockerized Postgres + Redis.

### 1. Start infrastructure (Postgres + Redis)

```bash
npm run infra:up          # starts TimescaleDB (5433), Redis (55000), Redis Commander (8081)
npm run infra:logs        # tail logs
npm run infra:down        # stop
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at least:

- `ANTHROPIC_API_KEY` (or `LLM_PROVIDER=bedrock` + AWS credentials)
- `JWT_SECRET` (use a real 32+ char secret outside dev)
- `INTERNAL_API_KEY` — shared secret guarding `/internal/jobs/*`; must match `worker-py`

### 3. Start the backend (NestJS API + WebSocket gateway)

```bash
npm run dev:backend
# or: cd apps/backend && npm install && npm run start:dev
```

Runs on `http://localhost:3009` by default. Migrations run automatically on startup.

### 4. Start the frontend (AstroJS dashboard)

```bash
npm run dev:frontend
# or: cd apps/frontend && npm install && npm run dev
```

Runs on `http://localhost:4331`.

### 5. Start the data worker (backfill + scheduler)

```bash
cd apps/worker-py
pip install -e .          # or: pip install -e ".[dev]" for tests
python -m worker.main
```

On startup it gap-fills missing history, then schedules periodic jobs. Set
`BACKFILL_ON_START=false` to skip the startup backfill.

---

## Running with Docker (full stack)

The project-level `docker-compose.yml` builds and runs **backend + frontend + worker-py**.
Database and Redis are **not** included here — point the apps at an external
Postgres/Timescale + Redis (e.g. `infra/docker-compose.yml` or a managed instance).

```bash
npm run infra:up          # bring up DB + Redis first
docker compose up --build # build & start the three app services
docker compose ps         # find the randomly-assigned host port for the frontend
```

Networking notes:

- Apps talk to each other by service name inside the compose network
  (`frontend → http://backend:3009`). Published host ports default to `0`, so Docker
  assigns random free ports — inspect them with `docker compose ps`.
- Only the **frontend** needs to be reachable from a browser; it proxies `/api` and
  `/socket.io` to the backend internally.
- On the host, DB/Redis are reached via `host.docker.internal` (overridable with
  `COMPOSE_DB_HOST` / `COMPOSE_REDIS_HOST`).

---

## Configuration

Key environment variables (see `.env.example` for the full list):

| Variable                              | Purpose                                            |
| ------------------------------------- | -------------------------------------------------- |
| `PORT` / `WS_PORT`                    | Backend HTTP / WebSocket ports (default 3009)      |
| `DATABASE_*`                          | Postgres/Timescale connection + `Algo_Trading` schema |
| `REDIS_HOST` / `REDIS_PORT`           | Redis connection                                   |
| `JWT_SECRET` / `JWT_EXPIRATION`       | Auth token signing + lifetime (default 7d)         |
| `LLM_PROVIDER`                        | `direct_api` (Anthropic) or `bedrock` (AWS)        |
| `ANTHROPIC_API_KEY`                   | Anthropic API key when using `direct_api`          |
| `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Bedrock credentials          |
| `INTERNAL_API_KEY`                    | Shared secret for `/internal/jobs/*` (backend ↔ worker) |
| `DEFAULT_DAILY_LOSS_LIMIT_PCT`        | Daily loss circuit-breaker (default 2.0%)          |
| `DEFAULT_MAX_CONCURRENT_POSITIONS`    | Max open positions (default 1)                     |
| `DEFAULT_PROBATION_SIZE_PCT`          | Reduced position size for new strategies (default 25%) |
| `DEFAULT_PROBATION_TRADES_COUNT`      | Probation trade count before full sizing (default 10) |

`worker-py` reads its own `.env` (`apps/worker-py/.env`) — notably `BACKFILL_SYMBOL`
(default `BTCUSDT`), `BASE_INTERVAL` (default `1m`), `BACKFILL_START` (default `2017-08`),
and `BALANCE_SYNC_INTERVAL_MINUTES` (default 5).

---

## Key Features

- **AI strategy generation** — LLM-generated trading strategies per ticker, with lessons
  learned fed back in, evaluation policies, and per-strategy performance tracking.
- **Multi-provider connectors** — register exchange providers, store credentials securely,
  toggle providers, switch trading mode (paper/live, testnet), sync balances, and inspect
  account state.
- **Hard risk guardrails** — configurable daily loss limits, max concurrent positions,
  hard stop-loss/take-profit, probation sizing for unproven strategies, and a per-provider
  **kill switch** to halt trading instantly.
- **Real market data** — full price-history backfill from `data.binance.vision`, stored in
  TimescaleDB hypertables, plus daily and startup gap-fills.
- **Capital allocation & reconciliation** — periodic rebalancing across strategies and
  broker-vs-ledger reconciliation reports.
- **Live dashboard** — real-time price chart, open positions, and an alerts feed streamed
  over Socket.IO (Redis → WebSocket bridge).
- **Cost visibility** — LLM usage is logged for cost summaries.

---

## Data Model (selected entities)

TypeORM entities live in `apps/backend/src/entities/`:

- **Market data:** `ticker`, `market-type`, `ohlcv-data` (Timescale hypertable),
  `data-quality-flag`
- **Trading:** `provider`, `provider-credential`, `provider-schedule`,
  `provider-balance-snapshot`, `order`, `position`, `risk-limit`, `daily-loss-tracking`
- **Strategy/AI:** `strategy`, `strategy-evaluation-policy`, `strategy-performance`,
  `backtest-result`, `ai-lesson-learned`, `live-vs-backtest-divergence`, `llm-cost-log`
- **Ops:** `allocation-snapshot`, `reconciliation-report`, `alert`, `user`

Migrations in `apps/backend/src/migrations/` are ordered and run automatically at startup.

---

## API Overview

REST endpoints (mounted under the backend base URL):

| Area          | Endpoints (selected)                                                     |
| ------------- | ------------------------------------------------------------------------ |
| `auth`        | `POST /register`, `POST /login`, `POST /forgot-password`, `POST /reset-password`, `GET /me` |
| `providers`   | `GET/POST /providers`, `PATCH /:id/toggle`, `PATCH /:id/trading-mode`, `GET /:id/balance`, `GET /:id/account`, `PATCH /:id/risk-limit`, `POST /:id/kill-switch`, `POST /:id/kill-switch/clear`, `POST /:id/sync-balance` |
| `market-data` | `GET /tickers`, `POST /tickers`, `GET /tickers/:id/candles`, `POST /tickers/:id/backfill` |
| `strategies`  | `POST /ticker/:id/generate`, `GET /ticker/:id/active`, `GET /ticker/:id/signals`, `POST /:id/activate`, `GET /:id/performance` |
| `execution`   | `GET /positions/open`, `GET /positions/all`, `POST /trade`, `POST /positions/:id/close` |
| `alerts`      | `GET /alerts`, `PATCH /:id/ack`                                           |
| `llm`         | `GET /cost-summary`                                                       |
| `internal/jobs` | `POST /balance-sync`, `/reconciliation`, `/allocation-rebalance`, `/alert-digest`, `/strategy-reevaluation`, `/data-quality` — **API-key guarded, called by worker-py** |

Real-time: Socket.IO gateway (`/socket.io`) streams live ticks and alerts to the dashboard.

---

## Scripts (root)

| Command                 | Description                                        |
| ----------------------- | -------------------------------------------------- |
| `npm run infra:up`      | Start Postgres + Redis (+ Redis Commander)         |
| `npm run infra:down`    | Stop infrastructure                                |
| `npm run infra:logs`    | Tail infrastructure logs                           |
| `npm run dev:backend`   | Start the NestJS backend in watch mode             |
| `npm run dev:frontend`  | Start the AstroJS dashboard                        |
| `npm run dev:worker`    | Start the Python worker                            |
| `npm run build:backend` | Build the backend                                  |
| `npm run build:frontend`| Build the frontend                                 |
| `npm run test:backend`  | Run backend tests (Jest)                           |

Backend-specific: `npm run migration:generate`, `npm run migration:run`,
`npm run migration:revert` (from `apps/backend`).

---

## Testing

```bash
npm run test:backend                 # NestJS / Jest unit tests
cd apps/worker-py && pytest          # worker tests (requires .[dev] extras)
```

---

## Additional Documentation

- **[DESIGN.md](DESIGN.md)** — product and UI design reference (dark terminal theme).
- **[PROGRESS.md](PROGRESS.md)** — build progress log.
- **[CONVENTIONS.md](CONVENTIONS.md)** — coding conventions.
- **[apps/worker-py/README.md](apps/worker-py/README.md)** — data worker details.
