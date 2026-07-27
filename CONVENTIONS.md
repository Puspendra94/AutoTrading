# Conventions

Established during the "remove mock data, wire real integrations" build (see `PROGRESS.md` for phase status). Follow these — don't impose a different style in a later session.

## Backend (`apps/backend`, NestJS)

- **Module layout**: one folder per feature under `src/modules/<name>/`, each with `<name>.module.ts`, `<name>.service.ts`, optionally `<name>.controller.ts`. Provider-specific exchange integrations live under `src/modules/provider/adapters/<exchange>.adapter.ts`.
- **Entities**: one file per table under `src/entities/`, re-exported from `src/entities/index.ts`. Column names `snake_case` in the DB via `@Column({ name: '...' })`, camelCase in TS.
- **Migrations**: TypeORM only, under `src/migrations/`. Never edit a migration that's already been committed — add a new one. Hand-written SQL only belongs inside a migration's `up()`/`down()` (e.g. `create_hypertable`), never as a standalone script.
- **Secrets**: everything in `.env` for infra/app-level config (DB, Redis, JWT, LLM keys). User-supplied exchange credentials go through `provider_credentials` via the `SecretsProvider` interface (`common/secrets/`) — never read that table directly. Currently `PlaintextSecretsProvider` (deliberate, see spec Section 9.1).
- **Auth**: every controller route is guarded with `@UseGuards(AuthGuard('jwt'))` unless explicitly public (register/login).
- **Error handling**: throw Nest's built-in exceptions (`NotFoundException`, `BadRequestException`, `ForbiddenException`) — don't invent a custom error envelope.
- **Real vs. simulated execution**: `Provider.tradingMode` (`'paper' | 'live'`) is the explicit, persisted safety switch — defaults to `'paper'`. Every order-placement code path must check it; never silently upgrade paper to live. `Provider.useTestnet` (default `true`) similarly gates whether `'live'` mode targets Binance's Spot Testnet or real mainnet — both must be explicitly flipped for real capital to move.
- **No silent fallbacks to fake data.** If a real API call fails, surface it (throw, flag `data_quality_flags`, mark onboarding `FAILED`) — never substitute synthetic/random data and continue as if it succeeded.
- **Background jobs**: owned by the standalone Python worker (`apps/worker-py`, APScheduler) — it is the single scheduler for the platform. It triggers each job via the backend's guarded `/internal/jobs/*` endpoints (`InternalJobsModule`) on the same cadences the retired in-process NestJS worker used, so the trading/AI logic stays in the one proven backend implementation. The backend API process (`main.ts`) never schedules jobs itself. The one exception is `MarketStreamService` — a persistent live WS connection that feeds `TradingGateway`, so it runs in the API process, not as a scheduled job.
- **Config**: every environment variable the backend reads is declared, typed, defaulted, and validated once in `src/config/configuration.ts`, which exports a frozen `config` singleton. Import `config` from there — nothing else in `src/` reads `process.env` directly. The worker mirrors this with `worker/config.py`.
- **Testing**: Jest `*.spec.ts` colocated with the service under test (see `risk-gate.service.spec.ts` as the existing pattern).

## Frontend (`apps/frontend`, Astro)

- No UI framework (React/Vue/Svelte) — plain Astro components + inline `<script>` blocks with TypeScript, matching the existing `index.astro` convention.
- Shared client logic lives in `src/lib/` (`api.ts` for REST + auth header injection, `ws.ts` for the Socket.IO singleton) — pages import from there, never duplicate `fetch`/socket setup per page.
- Reuse the existing design tokens/classes in `src/styles/global.css` (`.glass-card`, `.btn`, `.badge-*`, CSS custom properties) rather than introducing new ad hoc styles.
- `import.meta.env.PUBLIC_API_URL` / `PUBLIC_WS_URL` are the only source of the backend location — never hardcode `localhost:3009`.
- Auth: JWT stored in `localStorage`; `src/lib/api.ts` attaches it and redirects to `/login` on a 401.

## General

- Config values called out in spec Section 12 (risk limits, probation sizing, sync frequencies, etc.) are `.env`-driven with sane defaults, never magic numbers buried in a service — declared centrally in `src/config/configuration.ts` (backend) / `worker/config.py` (worker), not parsed ad hoc at each call site.
- Each service has its own `.env` loaded from its own directory (`apps/backend/.env`, `apps/worker-py/.env`, `apps/frontend/.env`, `infra/.env`); there is no root `.env`. Keep each `.env.example` in sync with the keys its service actually reads.
- Deviations from `algo-trading-platform-tech-spec.md` are recorded in `PROGRESS.md`'s "Deviations" section, with the reasoning — the spec is design intent, `PROGRESS.md` is where reality diverged and why.
