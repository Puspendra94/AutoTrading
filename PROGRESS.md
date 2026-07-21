# Progress

## Status: In progress — rebuilding real integrations to replace an initial mock-data scaffold

A prior agent (Gemini 3.5 Flash) built the initial `apps/backend`/`apps/frontend` scaffold from `algo-trading-platform-tech-spec.md`, but most integration points were faked rather than real (see "Starting state" below). This session's work is completing those per the plan in `algo-trading-platform-tech-spec.md`, starting with git/PROGRESS/CONVENTIONS hygiene (spec 0.4, skipped by the prior agent) and working through real Binance integration, background jobs, quant fixes, and a real frontend.

## Starting state (as found, before this session)

- Auth (JWT/bcrypt), DB schema (21/22 tables — `ai_lessons_learned` missing), risk gate, and LLM strategy generation (real Anthropic API call) were genuinely functional.
- Provider balance sync always returned a hardcoded `$10,000`; order execution never called Binance; historical backfill silently fell back to random-walk synthetic candles on failure; no live price streaming; no background/scheduled jobs anywhere (`@nestjs/schedule` installed but unused); reconciliation always reported "clean" (hardcoded empty mismatch list); allocation had no performance multiplier and no caller; frontend was 100% hardcoded static markup with no `fetch`/`WebSocket` calls and no login page.
- No git repo, no `PROGRESS.md`/`CONVENTIONS.md`.

## In progress this session

Working through the plan at `/Users/puspendrapandey/.claude/plans/groovy-marinating-falcon.md`:
- [ ] Stage 0 — git init, PROGRESS.md/CONVENTIONS.md (this commit)
- [ ] Stage 1 — real Binance adapter (balance, historical klines, live WS streaming, order placement), `Provider.tradingMode`/`useTestnet` safety switches
- [ ] Stage 2 — separate worker process, scheduled jobs (balance sync, reconciliation, re-evaluation, allocation rebalance, data quality, alert digest)
- [ ] Stage 3 — strategy evaluator quant fixes (real annualization, real Monte Carlo distribution stats, real regime segmentation, walk-forward validation)
- [ ] Stage 4 — real allocation performance multiplier, real reconciliation diff, notification wiring (+ optional Telegram)
- [ ] Stage 5 — `ai_lessons_learned` module (entity, migration, retrieval, wiring into generation/retirement)
- [ ] Stage 6 — frontend rewired to real REST/WebSocket data, login page, all four pages rebuilt

This section will be updated to reflect final status (Completed / Deviations / Next steps) once the above lands.

## Deviations from spec (tracked as they're made)

- **Worker uses `@nestjs/schedule` in a separate process, not BullMQ.** Spec Section 3/14.2 names BullMQ as an example job queue ("or equivalent"). Given single-user local scope, a second `NestFactory.createApplicationContext` process running cron jobs satisfies the "background jobs never block the live order path" architectural rule without adding Redis-backed queue infra. Revisit if retry/backoff semantics are ever needed.
- **Backend/worker are not containerized** (spec 14.2). Only infra tools (Postgres/TimescaleDB, Redis) run in Docker, as originally set up. Running `apps/backend` and the worker via `npm run dev:*` locally is sufficient to get real data flowing; Dockerizing the app layer is a reasonable next step, not done here.
- **`Provider.tradingMode` (`paper`/`live`) and `Provider.useTestnet`** are additions beyond the literal spec schema (Section 8.1's `providers` table doesn't list them) — added as the concrete safety mechanism for spec Section 0.2's checkpoint ("even paper-trading wiring is worth a human eyeballing once" before real order placement). Both default to the safe side (`paper`, `useTestnet=true`).

## Next steps (for whoever picks this up next)

- Real balance sync / order placement / reconciliation against live Binance data requires the user to enter real API credentials (testnet recommended first) via the Providers page — this hasn't happened yet as of this commit.
- Dockerizing `apps/backend` + worker into the same image with different start commands (spec 14.2) is not done.
- `ai_lessons_learned` retrieval is tag-based only (ticker + strategy-type), per spec's explicit "start simple" guidance — revisit with embedding/similarity search only if tag matching proves too coarse.
