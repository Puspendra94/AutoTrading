# Deployment — moving the dev environment to a 24/7 box

This is **not** a production deployment. The goal is to run exactly what runs on the laptop, on a
machine that does not sleep, so the loop can be observed over a long run. Bars the worker sleeps
through are never evaluated retroactively — that is the only reason this box exists.

Two compose files, and the split matters:

| File | Runs | Lifecycle |
|---|---|---|
| `infra/docker-compose.yml` | Postgres/TimescaleDB, Redis | started **once**, then left alone |
| `docker-compose.yml` (root) | backend, frontend, worker | rebuilt on **every** code change |

Redeploying the apps never stops or recreates the database container. (For the record, `docker
compose down` does not delete named volumes either — only `down -v` does — but keeping the stacks
separate means an app redeploy cannot even reach the database container.)

## First boot

```bash
docker compose -f infra/docker-compose.yml up -d     # once
docker compose up -d --build                         # every deploy after this
docker compose logs -f worker-py
```

Infra first: it creates the `algo_trading` network the app stack joins. The app stack declares that
network `external`, so the wrong order fails with a clear error instead of two stacks that cannot
see each other.

The first worker build compiles the TA-Lib C library from source, so allow a few minutes. The image
verifies `import talib` at build time — if the native library is missing, the build fails there
rather than at the first candle close.

## Configuration

Each app reads its own `.env` (`apps/backend/.env`, `apps/worker-py/.env`) exactly as it does when
run directly on your machine. Copy them to the box; that is the whole configuration step. No secret
is required for the stack to boot, and Redis runs without a password — the app code in this branch
has no `REDIS_PASSWORD` support, so enabling `requirepass` would break it rather than secure it.

Compose overrides only the DB/Redis wiring, because those are the only values that must differ
inside a container: the `.env` files say `localhost:5433` / `localhost:55000`, which is right for
running on the host and wrong for a container. Inside the network it is `postgres:5432` and
`redis:6379`. Remember `environment:` beats `env_file:` — anything pinned in compose cannot be
changed from a `.env`.

## Reaching it

Everything binds `127.0.0.1`. Nothing is published to the internet, because the dashboard has a
login form and no TLS, and the box holds exchange API keys. Tunnel in:

```bash
ssh -L 4321:127.0.0.1:4321 user@host     # dashboard -> http://localhost:4321
ssh -L 5433:127.0.0.1:5433 user@host     # psql / DBeaver -> 127.0.0.1:5433
```

Do not set `BIND_ADDRESS=0.0.0.0` to "make it easier". If you need real remote access, put a TLS
terminator in front (Caddy gets certificates automatically).

## Before you trust it

- **Check Binance is reachable from the box** — `curl -s -o /dev/null -w '%{http_code}\n'
  https://fapi.binance.com/fapi/v1/ping`. `200` is good; `451` means the region is blocked and
  nothing will trade. India (Mumbai) is fine; most US regions are not.
- **Do not restore a local database dump onto the box.** A dump carries `provider.trading_mode` and
  the stored API keys with it. The worker refetches the full candle history from
  `data.binance.vision` on first boot; an empty Postgres is the expected starting point.
- **Paper vs live is a database row** (`providers.trading_mode`), not an env var, so the box and
  your laptop are independent.
- `PATTERN_DECISIONS_ENABLED` in `apps/worker-py/.env` decides whether the worker may open
  positions. Watch a few `Evaluated 15m bar …` heartbeats land before turning it on.

## What is actually needed to trade

Postgres, Redis and the worker. With backend and frontend stopped, trading continues; the scheduled
jobs log a warning and skip. The one exception is **live** mode, where the backend's `balance-sync`
is what refreshes the account balance the risk gate sizes from.
