# Deployment

Two stages, in order. Infra first — it creates the Docker network the service stack attaches to.

These files are separate from `infra/docker-compose.yml`, which stays as the local-development
stack (publishes on `0.0.0.0` with default credentials — fine behind a laptop's NAT, unusable on
a public host).

---

## Step 0 — verify Binance is reachable BEFORE you commit to a host

Do this on the box, first, from an SSH session. It costs ten seconds and it is the one failure
that only shows up after everything else looks correct.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://fapi.binance.com/fapi/v1/ping
```

`200` — good. `451` — the host's region is blocked and **nothing will trade** from it; the REST
API and the market WebSocket will both refuse. Destroy it and pick another region.

India (Mumbai / Bangalore) is fine. Most US regions are not.

---

## Step 1 — infra

```bash
cd deploy
cp .env.example .env
# generate real values — do not invent them:
#   openssl rand -base64 32
$EDITOR .env

docker compose -f docker-compose.infra.yml up -d
docker compose -f docker-compose.infra.yml ps    # both should be (healthy)
```

Compose will refuse to start if `POSTGRES_PASSWORD` or `REDIS_PASSWORD` is unset. That is
deliberate: a public host must never come up with guessable credentials.

**Neither service is exposed to the internet.** Both publish on `127.0.0.1` only, and the apps
reach them by service name (`postgres`, `redis`) over the private `algo_trading` network. The
loopback publish exists so you can tunnel in:

```bash
ssh -L 5433:127.0.0.1:5433 user@host      # then psql to localhost:5433 as usual
```

### Data

Do **not** dump and restore. The local database is ~940 MB / 4.7M candle rows, and the worker
re-fetches the full history from `data.binance.vision` by itself on first boot
(`BACKFILL_ON_START=true`). Slower first start, zero migration work.

The schema is created automatically too — the backend runs its migrations on boot
(`apps/backend/src/main.ts`), so a completely empty Postgres is the expected starting point.

### Backups

Not configured. Once real money is involved, a nightly `pg_dump` to object storage is the
minimum. Positions and decisions are the irreplaceable part; candles can always be re-fetched.

---

## Step 2 — services

Not written yet. Blocked on one thing:

**`apps/worker-py/Dockerfile` installs four packages, hardcoded** — `asyncpg`, `httpx`,
`apscheduler`, `python-dotenv`. It predates all the AI and pattern work. Missing: `websockets`,
`redis`, `pydantic`, the four `langchain-*` packages, both `binance-*` connectors, `numpy`, and
**TA-Lib**.

TA-Lib is the awkward one: the Python package wraps a **C library** that is not in
`python:3.12-slim`, so it cannot simply be added to the pip line — the image needs build tools
and the native library compiled first. Everything else is one `pip install .`, since
`pyproject.toml` already lists the dependencies correctly.

### Minimum to trade 24/7

Postgres + Redis + **worker**. That is all.

The backend and frontend are needed only to create the schema once and to look at the UI. With
them down, trading continues — the scheduled jobs that call `/internal/jobs/*` log a warning and
skip.

One exception: in **live** mode keep the backend running. `balance-sync` is what refreshes the
real account balance the risk gate sizes from. Paper mode uses a fixed notional and does not
care.

### App env vars that change on the host

```
DATABASE_HOST=postgres      # service name, not localhost
REDIS_HOST=redis
REDIS_PASSWORD=<same value as deploy/.env>
```

`REDIS_PASSWORD` is required now that the deployed Redis sets `requirepass`. Both apps read it
and fall back to no-auth when it is empty, so local development is unaffected.
