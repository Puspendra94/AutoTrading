# Deployment

Two stacks, brought up in order:

| File | Runs |
|---|---|
| `docker-compose.infra.yml` | Postgres/TimescaleDB, Redis |
| `docker-compose.apps.yml` | backend, frontend, worker |

Infra first — it creates the `algo_trading` network the app stack joins. The app stack declares
that network as `external`, so starting it out of order fails with a clear error rather than two
stacks quietly not seeing each other.

Both are separate from `infra/docker-compose.yml`, which stays as the local-development stack
(publishes on `0.0.0.0` with default credentials — fine behind a laptop's NAT, unusable on a
public host).

---

## Step 0 — check Binance is reachable BEFORE paying for the host

Run this on the box, from an SSH session. Ten seconds, and it is the one failure that only shows
up after everything else looks correct.

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://fapi.binance.com/fapi/v1/ping
```

`200` — good. `451` — the region is blocked and **nothing will trade**; both the REST API and the
market WebSocket refuse. Destroy it and pick elsewhere. India (Mumbai / Bangalore) is fine; most
US regions are not.

---

## Step 1 — infra

```bash
cd deploy
cp .env.example .env
$EDITOR .env            # fill in every REQUIRED value — openssl rand -base64 32

docker compose -f docker-compose.infra.yml up -d
docker compose -f docker-compose.infra.yml ps      # both should read (healthy)
```

Compose refuses to start if a required secret is missing. That is deliberate: a public host must
never come up with guessable credentials.

**Neither service is exposed to the internet.** Both publish on `127.0.0.1` only; the apps reach
them by service name over the private network. The loopback publish is for tunnelling:

```bash
ssh -L 5433:127.0.0.1:5433 user@host     # then psql to localhost:5433
```

---

## Step 2 — apps

```bash
docker compose -f docker-compose.apps.yml up -d --build
docker compose -f docker-compose.apps.yml logs -f worker-py
```

The first build compiles the TA-Lib C library from source, so allow a few minutes. The image
verifies `import talib` at build time — if the native library is missing the build fails there
rather than at the first candle close.

The first boot then:

1. creates the schema (the backend runs its migrations on boot), and
2. backfills the full candle history from `data.binance.vision`.

That backfill is why you should **not** dump and restore your local database. It is ~940 MB /
4.7M rows for BTCUSDT 1m since 2017, and the worker fetches it itself. An empty Postgres is the
expected starting point.

### Watch it come up

```
TRADING_BRAIN=pattern
Opening live kline stream for 1 ticker(s): BTCUSDT@1m
Evaluated 15m bar … regime=… bias=… triggers=… warm=True
```

That last line is the per-bar heartbeat. Once it appears every 15 minutes, the loop is healthy.
`warm=False` right after a backfill is normal — the indicators need ~260 bars.

### Then turn on trading

`PATTERN_DECISIONS_ENABLED=false` is the default on purpose. The worker will stream, backfill and
record decisions without opening a single position. Watch a few bars land, then:

```bash
$EDITOR .env            # PATTERN_DECISIONS_ENABLED=true
docker compose -f docker-compose.apps.yml up -d worker-py
```

Nothing in the container reloads `.env` — it needs a restart, which the line above does.

### The dashboard

Published on `127.0.0.1` by default. The app has a login form and no TLS, so exposing it directly
would put your password on the wire in cleartext. Tunnel to it:

```bash
ssh -L 4321:127.0.0.1:4321 user@host     # then open http://localhost:4321
```

To expose it for real, put a TLS terminator in front (Caddy obtains certificates automatically).
Do not just set `FRONTEND_BIND=0.0.0.0`.

---

## What is actually required to trade

**Postgres + Redis + worker.** That is the whole 24/7 requirement.

The backend and frontend only create the schema once and serve the UI. With them stopped, trading
continues — the scheduled jobs that call `/internal/jobs/*` log a warning and skip.

One exception: in **live** mode keep the backend running. `balance-sync` is what refreshes the
real account balance the risk gate sizes from. Paper mode uses a fixed notional and does not care.

---

## Notes

**Startup order within the app stack.** The apps do not wait for infra to be healthy — compose
cannot express a dependency across stacks. The backend will crash if Postgres is not up yet and
`restart: unless-stopped` retries it until it is. Harmless, but expect a few restarts in the logs
on a cold boot.

**Internal vs published ports.** Inside the network Postgres is `5432` and Redis is `6379` — not
the `5433`/`55000` published on the host. Those are loopback-only, for tunnelling.

**Redis auth.** The deployed Redis sets `requirepass`, so `REDIS_PASSWORD` must match in both
stacks. Both apps fall back to no-auth when it is empty, which is what keeps local development
working unchanged.

**Backups.** Not configured. Once real money is involved, a nightly `pg_dump` to object storage is
the minimum. Positions and decisions are the irreplaceable part — candles can always be re-fetched.

**Machine sleep.** This is the reason to deploy at all: bars the worker sleeps through are never
retroactively evaluated, so a laptop only ever covers the hours it is awake.
