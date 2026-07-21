# AI-Based Algorithmic Trading Platform — Technical Specification

**Version:** 1.0
**Status:** Design phase — ready for implementation
**Purpose of this document:** This is a complete, self-contained technical specification for a multi-provider, AI-assisted algorithmic trading platform. It is written to be handed directly to an engineer or an LLM-based coding assistant to implement, without requiring additional context from prior discussions. Every design decision includes the reasoning behind it so implementers understand *why*, not just *what*.

---

## 0. Instructions for the Implementing Agent

**If you are an agentic coding tool (with real shell/file access) reading this file: this document is your sole source of truth. Read it in full before taking any action.** No prior conversation, chat history, or external context exists beyond what's written here — everything needed to build this project is in this file.

### 0.1 Order of operations

0. **Check for and read `PROGRESS.md` and `CONVENTIONS.md` at the repo root first, if they exist** (Section 0.4) — this project is built across multiple agent sessions, and these files tell you what's already done and what conventions are already established. If they don't exist yet, you're the first agent — create them as part of Phase 1 (Section 11) before writing feature code.
1. **Section 13.0 first** — create the folder structure (`algo-trading-platform/apps/frontend`, `apps/backend`, `infra/`) and `git init`, before anything else.
2. **Section 13.1–13.5** — bring up the infra tools (Postgres/TimescaleDB, Redis) via Docker Compose. **Requires Docker Desktop to already be running on the machine** — if it isn't, stop and tell the user, don't attempt to install or start it yourself.
3. **Section 15.1** — scaffold `apps/frontend` (Astro, design skills, MCP config) exactly in the order listed.
4. **Section 11's phased build order** for `apps/backend` — Phase 1 (Foundation) through Phase 5 (Mode B/polish), in sequence. Don't skip ahead to later phases (e.g., live strategy generation, real order placement) before earlier ones (auth, provider connection, risk gate skeleton) are working and testable.
5. **Section 16** for the main dashboard, once there's live data and strategy state to actually display.

### 0.2 Checkpoints — stop and confirm with the user before proceeding

- **Before Phase 2 of Section 11** (real order placement against a live provider, even in small/test size) — even paper-trading wiring is worth a human eyeballing once, since a misconfigured risk gate (Section 5) is exactly the kind of mistake that should be caught before any capital, real or simulated, moves.
- **Before writing any value into `.env`** that the user hasn't explicitly provided (API keys, secrets) — never invent, guess, or use placeholder values in a way that could silently end up live.
- **Before any destructive action** (`docker compose down -v`, dropping a database, force-pushing git history, deleting existing project files).

### 0.3 Operating boundaries

- Operate only within the `algo-trading-platform/` project directory created in Section 13.0. Do not modify, read from, or write to other projects on the machine — including the user's existing, separate Postgres instance on port `5432` (Section 13.2), which this project does not touch.
- Follow the "nothing hardcoded, everything via `.env`" rule (Section 9.1) for every credential, connection string, and secret — no exceptions, even for local-only convenience.
- Follow the migrations-only schema rule (Section 14.1) — never hand-write or hand-run SQL against the database outside of a committed migration file.
- Keep `apps/frontend` and `apps/backend` fully decoupled (Section 13, project structure note) — separate `package.json`s, no shared dependency tree, even though both live in one repo.

### 0.4 Continuity across agents and sessions — this is not a one-agent project

**This project is expected to be picked up, continued, and handed off across multiple different agents/LLMs over time — never assume you are the only implementer or that another agent will have access to this conversation, your reasoning, or any memory of what you did.** The only things that persist between sessions are what's committed to the repo: code, this spec, and two specific tracking files described below. Every agent — including the first one — is responsible for keeping these current, not just writing code.

**Required file 1 — `PROGRESS.md` at the repo root.** This is the living record of implementation state, distinct from git commit history (commits show *what* changed; this file shows *where things stand and why*). Structure it around the phases in Section 11:

- **Completed** — which phases/features are done and working, with enough detail that another agent doesn't need to re-derive it by reading all the code (e.g., "Phase 1 complete: auth, provider connection for Binance, ticker CRUD, historical backfill. Balance sync tested manually, not yet automated on a schedule.").
- **In progress** — what's partially built, and specifically what's *not* finished about it (a half-built feature is more dangerous for the next agent to misjudge than a missing one).
- **Deviations from this spec** — anywhere the actual implementation diverged from what this document says, and why (spec assumptions that turned out to be wrong, library limitations discovered, etc.). This spec is the design intent; `PROGRESS.md` is the record of where reality diverged from it.
- **Next steps** — what the next agent should logically pick up, per Section 11's build order.

**Every agent must read `PROGRESS.md` in full before writing any code, and must update it before ending its session** — even mid-phase, even if the work isn't "done," so the next agent (human or LLM) always has an accurate picture.

**Required file 2 — `CONVENTIONS.md` at the repo root.** Different LLMs/agents have different default coding styles, naming habits, and structural preferences; without an explicit, authoritative record, a project touched by multiple agents drifts into inconsistency. The first agent to write real code establishes this file (naming conventions, folder/module structure patterns actually used, error-handling style, testing approach, any deviations from generic NestJS/Astro defaults) and every subsequent agent **follows what's already there rather than imposing its own preferences** — treat it the same way you'd treat an existing team's style guide, not a suggestion.

**When adding any agent-facing config (skills, MCP servers, `AGENTS.md`/similar agent-instruction files)** — beyond what's already specified in Section 15.1 — document what was added and why in `PROGRESS.md`, so it's visible to the next agent rather than silently discovered.

---

## 1. Product Vision

A single web-based platform where a user can:

1. Connect one or more trading providers/exchanges (e.g., Binance, Zerodha/Kite).
2. Add tickers/symbols they want algorithmically traded.
3. Let the system automatically research, generate, backtest, and deploy trading strategies for those tickers using AI (LLM-assisted) and classical quantitative methods.
4. Have the system manage risk, position sizing, and capital allocation across all managed tickers within a provider, continuously monitor strategy health, and replace underperforming strategies automatically — while keeping the user informed via alerts.
5. Monitor everything (P/L, positions, strategy performance, system health) from a single dashboard, on desktop/web.

**Explicitly out of scope (by deliberate decision, see Section 2):**
- Managing the user's pre-existing/long-term holdings in a provider account. The platform only manages capital and positions that the user explicitly adds for algorithmic trading.
- Mobile native or PWA support (desktop/web only for now).
- Production-grade secrets encryption (deferred until the system goes beyond local/single-user use — see Section 9).

---

## 2. Key Design Decisions (and why)

These decisions were deliberately made to keep the system's complexity proportional to what it actually needs to do. Read this section before implementing anything — it explains *why* the schema and modules in later sections look the way they do.

### 2.1 Only manage capital and positions explicitly added to the platform

Early design considered pulling and managing a user's **entire** provider portfolio (including pre-existing long-term holdings, manually placed trades, etc.), with a `managed`/`unmanaged` toggle per holding.

**This was deliberately dropped.** Reasoning: a user's existing long-term holdings are not algo-trading candidates by nature (they're long-term, not systematically traded), so pulling, tracking, and reconciling them added significant complexity (portfolio sync, toggle state, transition snapshotting, cost-basis tracking) for no benefit to the platform's actual purpose.

**What the system does instead:** on connecting a provider, it pulls only the **tradable/available balance** (cash or free margin) — not a full portfolio. This balance is the capital pool the platform allocates from. Pre-existing holdings in the provider account are invisible to the platform and never touched.

### 2.2 Provider-wide capital pool, dynamically allocated

Capital is not manually assigned per ticker. When a user connects a provider (e.g., Binance) with a tradable balance, and adds multiple tickers for AI management, **the system itself decides how to split that capital across tickers/strategies, and rebalances over time** based on strategy performance (see Section 6, Allocation & Portfolio Management).

### 2.3 Risk management is a hard gate, not a soft LLM judgment

All risk limits (daily loss cap, max concurrent positions, position sizing) are enforced by **deterministic code**, sitting between any strategy/AI decision and actual order placement. The AI never has a path to bypass these limits. This is critical: LLM-generated or LLM-driven trading decisions must pass through the same risk gate as everything else, with zero exceptions.

### 2.4 Strategy "goodness" is computed, not judged by the LLM

An LLM proposes strategy logic. Whether that strategy is actually good (statistically robust, not overfit, acceptable risk profile) is decided by an independent, deterministic **Strategy Evaluation Module** that computes hard metrics against configurable thresholds. See Section 7 for the full criteria — this directly encodes standard quant strategy-evaluation practice (risk-adjusted returns, drawdown behavior, out-of-sample validation, overfitting checks, execution realism, regime robustness, statistical significance) so that "is this strategy good" is never left to a model's subjective judgment.

### 2.5 No human-approval gate before going live — alert-only, with probation sizing as the real safety net

Waiting for manual approval before a new strategy goes live was considered and rejected: if the system is running unattended and detects that an existing strategy is failing, a mandatory wait-for-approval step could let losses accumulate while the user is away. Instead:

- A new strategy that passes the Strategy Evaluation Module gate is **auto-promoted to live**, replacing the failing one.
- An alert fires immediately with full context (why the old strategy was flagged, the new strategy's backtest stats).
- The new strategy trades at **reduced position size ("probation sizing")** for its first N trades / M days live, only scaling to full allocation once it holds up in live conditions. This is the actual downside protection mechanism, not a human click.
- The daily loss cap (Section 5) is the final backstop regardless.

### 2.6 Two strategy execution modes (not everything needs to call an LLM live)

- **Mode A — Rules-Engine Execution (default):** the LLM/AI is used *offline* to generate strategy logic and parameters. Once generated, a deterministic rules engine executes trades in real time with **zero LLM calls in the live decision path.** Fast, cheap, predictable, fully auditable.
- **Mode B — AI-Live-Decision Execution:** for specific tickers/strategies explicitly flagged for it (e.g., discretionary or news-sensitive assets where live market context genuinely benefits from LLM reasoning), the AI is called at decision time to make live trading calls. This has real per-decision token cost and latency.

Mode A is the default for all tickers. Mode B is opt-in per ticker/strategy. This keeps AI cost bounded and keeps the execution hot path fast for the majority of the system, while still allowing AI-driven live trading where it adds real value.

### 2.7 Numerical work and LLM work are strictly separated

Historical data processing, feature engineering, and backtesting over years of OHLCV data are done with **pure numerical/statistical code** (pandas/numpy-equivalent, vectorized backtesting libraries), never by feeding raw price data into an LLM's context window. The LLM only ever sees **summarized, structured statistics** (metrics, not raw ticks) when proposing or evaluating strategies. This keeps token usage small and bounded regardless of how much historical data exists, and matches how LLMs are actually good at reasoning (over compact structured input) versus what they're bad at (numerical pattern-mining over millions of raw rows).

### 2.8 Database: PostgreSQL + TimescaleDB, not a separate time-series database

The user already runs PostgreSQL in Docker. Recommendation: add the **TimescaleDB extension** rather than introducing a second database system.

- TimescaleDB is a Postgres extension, not a separate database — all existing Postgres tooling, ORM, and auth integration carry over unchanged.
- Auto-partitions time-series data (OHLCV candles) into hypertables for fast range queries at scale.
- Built-in continuous aggregates can auto-roll 1-minute candles into 5m/1h/1d timeframes without a separate ETL job.
- Compression policies shrink historical "cold" data significantly.
- Only reconsider (e.g., ClickHouse) if tick-level, many-symbol throughput becomes an actual bottleneck — do not over-engineer this upfront.

Add **Redis** as a cache/pub-sub layer for live price, open positions, and dashboard push updates — not as a system of record.

**Deliberately deferred: primary/replica (master-slave) database architecture.** A 1-primary + 2-replica setup (backend and worker reading from separate replicas, or load-split between them) was considered and explicitly deferred, not rejected — revisit if read load actually becomes a bottleneck on a single instance. Reasoning: at current scale (single local user), the operational cost (replication setup, connection routing, replica lag monitoring, failover handling) isn't justified yet. If this is revisited later, note the correctness constraint it must respect: **risk-critical reads (the risk gate in Section 5 — daily loss limit checks, capital-under-management calculations) must always read from the primary, never a replica**, since asynchronous replication lag could let the risk gate approve an order against stale capital/P&L data. Any future read/write split should be **query-type-based** (risk-critical vs. everything else) inside the ORM's data source layer, not a simple "backend reads from replica A, worker reads from replica B" split — the backend does both risk-critical and non-critical reads itself.

### 2.9 Modular monolith, not microservices

Given the scope (auth, multi-provider integration, scheduling, live trading, AI strategy generation, backtesting, dashboard), a **modular monolith** (single deployable, cleanly separated internal modules) is the right starting architecture. Peel out a service only if a specific piece (e.g., market-data ingestion) genuinely needs independent scaling later.

---

## 3. High-Level Architecture

```
                                   ┌─────────────────────────┐
                                   │   Frontend (Web/Desktop)│
                                   │   AstroJS + WS client    │
                                   └────────────┬─────────────┘
                                                │ REST + WebSocket
                                   ┌────────────▼─────────────┐
                                   │      API Gateway Layer    │
                                   │  (NestJS, in-app JWT)     │
                                   └────────────┬─────────────┘
        ┌─────────────────┬──────────────────┼──────────────────┬────────────────────┐
        │                  │                  │                  │                    │
┌───────▼──────┐  ┌────────▼───────┐ ┌────────▼────────┐ ┌───────▼────────┐ ┌─────────▼─────────┐
│ Provider Mgmt │  │ Market Data     │ │ Strategy Engine  │ │ Risk & Execution│ │ Notification/Alert │
│ Module        │  │ Module          │ │ Module (AI +     │ │ Module          │ │ Module              │
│               │  │ (ingestion,     │ │ Rules Engine)    │ │ (gate, orders,  │ │                      │
│               │  │ TimescaleDB)    │ │                   │ │ allocation)     │ │                      │
└───────┬──────┘  └────────┬───────┘ └────────┬────────┘ └───────┬────────┘ └─────────┬─────────┘
        │                  │                  │                  │                    │
        └──────────────────┴──────────────────┴──────────────────┴────────────────────┘
                                                │
                              ┌─────────────────┴──────────────────┐
                              │   PostgreSQL + TimescaleDB (data)   │
                              │   Redis (cache / pub-sub / queue)   │
                              └──────────────────────────────────────┘

  Background workers (separate processes via job queue — BullMQ or equivalent):
  - Balance sync worker (per provider, scheduled)
  - Reconciliation worker (scheduled, non-blocking, writes report)
  - Strategy generation / re-evaluation worker (scheduled, daily + on new-ticker onboarding)
  - Allocation/rebalance worker (scheduled, daily)
  - Data quality monitor worker (on ingestion + scheduled)
```

**Critical architectural rule:** the live order-execution path (Risk & Execution Module) must never block on or wait for AI/LLM calls, except for tickers explicitly configured for Mode B. All AI work for Mode A tickers happens asynchronously, ahead of time, in background workers.

---

## 4. Core Modules

### 4.1 Auth & User Module
- **In-app JWT authentication** — no external identity provider (Keycloak deliberately dropped to avoid unnecessary operational complexity for a single-user local system). Standard flow: username/password → server validates against `users` table (hashed password, e.g. bcrypt/argon2) → issues a signed JWT (access token, short-lived) + refresh token → client sends JWT on subsequent requests via `Authorization: Bearer` header → NestJS guard validates signature/expiry on each request.
- JWT signing secret is read from environment configuration (`JWT_SECRET`), never hardcoded — see Section 9.1.
- Single-user system for now, but design the schema to support multiple users later (don't hardcode single-user assumptions into core tables).
- If multi-user support is added later, this module is the only one that needs to grow (e.g., adding roles/permissions) — nothing else in the system should assume a specific auth mechanism, since all other modules only care about "is this request authenticated," not how.

### 4.2 Provider Management Module
Responsibilities:
- Add/edit/remove a provider (e.g., Binance, Zerodha/Kite).
- Store provider API credentials (plaintext for now — see Section 9).
- On connect: verify credentials, pull tradable/available balance only (not full portfolio — see Section 2.1).
- If a provider's API integration isn't implemented yet, show a clear message (e.g., "Coming soon — starting with Binance first") rather than a generic error.
- Scheduled balance sync (frequent enough that the risk gate and allocation layer are working off near-real-time capital — recommend every few minutes during market hours, configurable).
- Provider-level scheduling config: e.g., a provider like Kite/Zerodha is only active weekdays 9am–3pm (its exchange hours); Binance is active 24/7. This scheduling config gates whether trading is even attempted for tickers under that provider.
- Global and per-provider trading on/off switch.

#### 4.2.1 Binance integration specifics

Binance is the first provider (Section 4.2, and the onboarding flow in Section 4.6). Two things whoever implements this integration should use directly, rather than reconstructing Binance's API surface from general knowledge:

- **Official Binance SDK** — use Binance's own SDK for all API calls (REST + WebSocket), rather than hand-rolling HTTP requests against their endpoints. This gets you correct request signing, rate-limit handling, and WebSocket reconnection logic for free, and stays current as Binance evolves its API.
- **Binance's own LLM-context documentation** — Binance publishes documentation specifically formatted for LLM consumption, which whoever builds this integration (human or LLM) should pull in as context before writing any Binance-facing code, rather than relying on general/training-data knowledge of Binance's API (which may be stale):
  ```bash
  # Condensed context
  curl -s https://developers.binance.com/en/docs/llms.txt

  # Full documentation for comprehensive context
  curl -s https://developers.binance.com/en/docs/llms-full.txt
  ```
  This is the same reasoning as the Astro docs MCP server in Section 15.1 — pull current, authoritative documentation directly rather than trusting potentially outdated model knowledge for an API surface that changes over time.

### 4.3 Market Data Module
Responsibilities:
- Fetch list of market types supported by each provider (e.g., spot, futures, options).
- Add tickers/symbols per market type, with interval (1m, 5m, 1h, 1d, etc.).
- On new ticker added: automatically pull historical data since the last stored data point, or full historical backfill if none exists (see Section 4.6, Ticker Onboarding Flow).
- Store all market data (OHLCV) in TimescaleDB hypertables.
- Real-time ingestion via provider WebSocket connections where available, feeding a queue (Redis Streams or similar) → TimescaleDB writer → live cache update → WebSocket push to frontend.
- Data quality monitoring (see Section 4.7).

### 4.4 Strategy Engine Module
Responsibilities:
- Generate trading strategies for a ticker using a combination of classical quant methods (over full historical data, no LLM involved) and LLM reasoning (over summarized statistics only — see Section 2.7).
- Strategy versioning: every generated strategy is a new version, never overwritten. Old versions retain their full parameter set, backtest results, and live performance history for comparison/rollback.
- Strategy Evaluation Module: independently, deterministically scores every generated strategy against the criteria in Section 7, before it's eligible for promotion.
- Daily (or interval-based) re-evaluation of every live strategy's actual performance vs. its backtest expectation (divergence tracking, Section 7.5). If a strategy is flagged as failing, trigger regeneration.
- Supports both Mode A (rules-engine execution) and Mode B (AI-live-decision execution) per ticker — see Section 2.6.

### 4.5 Risk & Execution Module
Responsibilities:
- **The single gate every order must pass through**, regardless of what generated the trade signal (Mode A rules engine, Mode B live AI decision, or manual override).
- Enforces (see Section 5 for full detail):
  - Max daily loss limit: 2% of capital currently under management, per provider.
  - Max 1 concurrent open position per ticker/symbol.
  - Position sizing from the Allocation Module's current ceiling for that ticker.
  - Probation sizing for newly promoted strategies.
- Places orders via provider API, tracks order lifecycle (pending → filled/partial/rejected → closed).
- Allocation/Portfolio Layer (see Section 6): decides how much of a provider's capital pool goes to each ticker/strategy, rebalanced on a schedule. Sits above the per-ticker risk gate — allocation decides the ceiling, the gate enforces it at order time.

### 4.6 Ticker Onboarding Flow
When a new ticker is added, before any live trading starts, the system runs this pipeline in order:

1. **Fetch historical data** — as much as available, or at minimum enough to generate a statistically meaningful strategy (tie to the minimum trade count threshold in Section 7).
2. **Generate alpha/strategy** for that ticker (Strategy Engine Module).
3. **Generate trading rules** (concrete entry/exit logic, position sizing inputs) from the strategy.
4. **Backtest the strategy** using walk-forward validation (Section 7.2), including realistic slippage/fees (Section 7.4).
5. **Pass through the Strategy Evaluation Module gate** (Section 7) — only strategies clearing configured thresholds proceed.
6. **Start trading** — at probation sizing initially (Section 2.5), scaling to full allocation after the probation period holds up.

If a provider's API is not yet integrated (Section 4.2), this flow halts at step 1 with a clear message to the user.

### 4.7 Data Quality Monitoring
Runs on ingestion and on a schedule:
- Detect gaps in historical/live series.
- Detect duplicate candles.
- Detect anomalous price spikes (distinguish bad ticks from real volatility — flag for review rather than silently dropping).
- Detect timezone/DST inconsistencies.
- Flags are stored (`data_quality_flags` table) and surfaced via alerts if severe (e.g., a gap large enough to affect strategy generation for a ticker currently being onboarded or re-evaluated).
- Strategy generation and backtesting should refuse to proceed (or clearly flag reduced confidence) if the underlying data has unresolved quality flags.

### 4.8 Reconciliation Module
- Runs on a **separate background worker/process** (via job queue), explicitly not blocking the main application thread or the live trading path.
- Periodically compares the platform's recorded positions/orders against the provider's actual account state (via provider API).
- Writes results to a `reconciliation_reports` table.
- Only fires an alert on mismatch — clean runs don't generate noise.
- This is distinct from balance sync (Section 4.2): balance sync answers "how much tradable capital is available," reconciliation answers "do my recorded positions/orders match what the provider actually shows."

### 4.9 Notification / Alert Module
Three severity tiers, so alerts don't become noise the user tunes out:
- **Critical** (immediate push/Telegram/email): daily loss limit hit, kill switch triggered, provider API auth failure, reconciliation finds a serious mismatch.
- **Warning** (can batch into a digest): strategy underperforming and being replaced, data quality flag raised, minor reconciliation mismatch.
- **Info** (in-app only): new strategy promoted, ticker onboarding completed.

### 4.10 LLM Cost Tracking Module
- Every LLM call is logged: ticker, purpose (strategy generation / daily re-evaluation / live Mode-B decision), model used, input/output tokens, computed cost.
- Tied back to the resulting strategy/trade so the system can compute **cost-adjusted P/L per ticker** — a strategy profitable before AI costs but not after needs to be visible as such.
- LLM provider must be configurable — direct Anthropic API vs. Amazon Bedrock — via a provider-agnostic interface (see Section 9.2).
- Optional: a configurable budget cap per ticker/day to prevent runaway strategy-regeneration loops from accumulating unexpected cost.

### 4.11 AI Memory & Continuous Learning Module

**This is one of the most important modules in the system, and easy to underbuild.** Without it, every strategy generation and every Mode B live decision starts from zero — the AI has no way to know it already tried something similar for this ticker (or a similar one) and it failed, so it can repeat the same mistake indefinitely. This module closes that loop: every strategy outcome (success or failure) is distilled into a durable, queryable lesson, and future AI-driven decisions are required to consult relevant lessons before acting.

**This is distinct from `PROGRESS.md`/`CONVENTIONS.md` in Section 0.4** — those track *implementation* continuity across coding agents. This module tracks *trading/strategy* continuity across the AI's own decisions, so the system itself gets better over time, not just the codebase.

#### 4.11.1 What gets captured as a "lesson"

Triggered automatically at the same points strategies are already being evaluated (ties directly into Section 2.5's auto-promotion/replacement flow and Section 7.5's divergence tracking) — whenever a strategy is retired, replaced, or a Mode B live decision sequence closes out with a result:

- A summarization LLM call reviews that strategy/decision's full outcome (backtest expectations vs. what actually happened live, the divergence, market regime at the time, parameters used) and distills it into a **structured, generalized lesson** — not a raw dump of trade logs. E.g.: "Mean-reversion strategies with a lookback window under 10 periods overfit on BTCUSDT during low-volatility regimes — backtest Sharpe looked strong but live divergence exceeded threshold within 2 weeks," rather than a full trade-by-trade history.
- Each lesson is tagged for retrieval: ticker, asset class/market type, strategy type/approach, regime conditions, and the specific failure or success mode identified.
- Successes are captured too, not just failures — "what worked and why" is just as valuable for future generation as "what didn't."

#### 4.11.2 How lessons get used

- **Strategy generation (Section 4.4, Ticker Onboarding Flow Section 4.6):** before generating a new strategy for a ticker, the system retrieves relevant past lessons (matching ticker, similar asset type, or similar regime/strategy-type tags) and includes a **summarized** version of them in the LLM's generation prompt — enough to steer it away from previously-failed approaches, without bloating the prompt with raw history. This follows the same token-efficiency principle as Section 2.7: summarized structured input, never raw logs.
- **Daily re-evaluation (Section 2.5):** when a strategy is flagged as failing and a replacement is being generated, the specific lesson from *that* failure is guaranteed to be included in the replacement's generation context — this is the most direct "don't make the same mistake twice" path.
- **Mode B live decisions (Section 2.6):** relevant lessons for that ticker are included in the live-decision prompt context where relevant, subject to the same token/cost discipline as everything else in Mode B.
- Retrieval doesn't need to be sophisticated on day one — start with tag-based filtering (ticker + strategy-type + regime), and only reach for embedding/similarity search later if tag matching proves too coarse. Consistent with Section 6's allocation-policy guidance: start simple, iterate.

#### 4.11.3 Storage

See Section 8.5 for the `ai_lessons_learned` table. Every lesson links back to the `strategy_id`/`backtest_results`/`live_vs_backtest_divergence` records it was derived from, so the reasoning behind a lesson is always traceable, not just the summarized conclusion.

**Persistence and growth:** lessons live in the same Postgres/TimescaleDB instance as everything else (Section 13.2) — not a separate database, not remote storage — and persist across application/container restarts via the Docker-managed volume, same as all other application data. They're only lost if that volume is explicitly deleted (`docker compose down -v`). Because lessons are stored as summarized text (Section 4.11.1), not raw trade/decision logs, this table grows slowly and predictably — negligible compared to the OHLCV time-series data, which is the actual storage driver in this system and is already addressed by TimescaleDB compression (Section 2.8). No separate storage system (S3, a second database, etc.) is warranted for this at the scale this system is designed for; revisit only if a concrete need arises (e.g., portable backup independent of the main database).

---

## 5. Risk Management — Full Specification

This is the most safety-critical part of the system and must be implemented as deterministic, unbypassable code sitting between any decision-maker (rules engine, AI, or manual action) and actual order placement.

### 5.1 Daily loss limit
- **Limit:** 2% of capital currently under management, **per provider**.
- "Capital under management" = the sum of capital the platform has allocated across all managed tickers for that provider (from the Allocation Module, Section 6), recalculated live before every risk-gate check — not a cached/stale number.
- Tracked as realized + unrealized P/L against this base, reset at a clearly defined boundary (recommend: provider's exchange trading-day boundary if applicable, otherwise UTC midnight — this should be an explicit, documented, per-provider config value, not implicit).
- **On breach:** auto-flatten all open managed positions for that provider, hard-block any new managed order for that provider until the next reset boundary, fire a Critical alert immediately.

### 5.2 Max concurrent positions
- **Limit:** max 1 open position per ticker/symbol at a time.
- Enforced at the order-gate level (not just at the strategy layer), so even if two different sources (e.g., a Mode A rules engine and a manual override) attempt to open a second position on the same ticker, the gate rejects the second order outright.

### 5.3 Position sizing
- Comes from the Allocation Module's current ceiling for that ticker (Section 6), further reduced by probation sizing if the strategy is newly promoted (Section 2.5).
- The risk gate enforces the sizing ceiling at order-placement time; it does not compute allocation itself (separation of concerns between "how much should this ticker get" and "is this specific order allowed").

### 5.4 Kill switch
- Any Critical-tier condition (daily loss limit breach, reconciliation finding a serious/unexplained discrepancy, repeated provider API failures) should be able to trigger a global or per-provider kill switch that halts all new order placement until manually cleared or the next scheduled reset, whichever is appropriate to the trigger.

---

## 6. Allocation & Portfolio Management

Since capital is a **provider-wide pool** dynamically split across managed tickers (Section 2.2), this module sits logically above the per-ticker risk gate:

- **Input:** provider's current tradable balance (from balance sync), the set of active managed tickers/strategies for that provider, each strategy's backtest confidence metrics (Section 7) and live performance (Section 7.5).
- **Policy (start simple, iterate later):** recommend starting with an **equal-weight allocation across active managed tickers, adjusted by a performance multiplier** (e.g., strategies with stronger recent Sharpe/live performance get a modestly larger share, weak/probationary strategies get less). Avoid building a complex portfolio-optimization model on day one — this is easy to over-engineer; a simple deterministic policy that's easy to reason about and adjust is the right starting point.
- **Diversification constraint:** no single ticker should be allowed to dominate the pool (set a configurable max % per ticker).
- **Rebalance schedule:** runs daily, alongside the strategy re-evaluation step.
- **How rebalancing takes effect:** rebalancing changes the *allowed position-sizing ceiling for new trades going forward*. It does **not** forcibly liquidate or resize existing open positions — those ride out under their prior allocation until they close naturally, unless a hard risk trigger (Section 5) forces earlier action. This avoids disruptive, unnecessary churn from the rebalancing process itself.

---

## 7. What Makes a Strategy "Good" — Strategy Evaluation Criteria

This section is the deterministic gate every generated strategy must pass before promotion to live trading (whether initial onboarding or replacing a failing strategy). This is implemented as an independent Strategy Evaluation Module — **the LLM proposes strategy logic; this module computes the numbers and applies the gate.** "Is this a good strategy" must never be left as a subjective LLM judgment call.

### 7.1 Risk-adjusted return metrics (required, computed and stored per strategy)
- **Sharpe Ratio** — return per unit of volatility. Configurable minimum threshold (recommend starting around 1.0; below 0.5 should be an automatic fail).
- **Sortino Ratio** — like Sharpe but only penalizes downside volatility.
- **Calmar Ratio** — annual return divided by max drawdown.

### 7.2 Backtest validity requirements
- **Out-of-sample / walk-forward validation is mandatory.** A strategy's reported backtest stats must never be computed on the same data window the strategy/parameters were tuned against. In-sample-only results are treated as invalid and cannot be used for a promotion decision.
- **Minimum trade count threshold** (recommend 100+ trades) before any backtest stats are trusted. Strategies with fewer trades are flagged as statistically meaningless regardless of how good the numbers look, and cannot be promoted.
- **Multiple market regimes tested** — trending, choppy/sideways, high-volatility, low-volatility periods, using distinct historical windows. A strategy that only performs well on the exact window it was tuned on must be flagged, not promoted.
- **Overfitting check** — flag/penalize strategies with a high number of tunable parameters; prefer simpler logic that's more likely to generalize. This should factor into the promotion score, not just be a warning.
- **Look-ahead bias check** — verify the strategy never uses information it would not have had available at decision time in a live setting (e.g., using a candle's close to decide an action that would have needed to happen at that candle's open).

### 7.3 Drawdown characteristics (required, computed and stored per strategy)
- **Max drawdown** — worst peak-to-trough decline. Configurable maximum threshold for promotion.
- **Drawdown duration** — time to recover from the worst drawdown. Long recovery times should reduce a strategy's promotion score even if max drawdown itself is within limits.
- **Drawdown frequency** — distinguish a strategy with one bad period from one that grinds through frequent losing streaks.

### 7.4 Execution realism
- Backtests must simulate **slippage and transaction costs/fees** — never assume frictionless execution.
- Liquidity constraints should be considered for position sizes relative to the ticker's typical volume, where data allows.

### 7.5 Statistical significance and live tracking
- **Profit factor** (gross profit / gross loss) — configurable minimum threshold (recommend starting around 1.3–1.5).
- **Monte Carlo trade-reshuffling** — reshuffle the order of backtest trades and re-check the resulting stats' sensitivity, to catch strategies that only look good because of the specific sequence of trades they happened to get.
- **Win rate is tracked but never used alone** — a low win-rate/high-reward strategy can be very profitable (trend-following) and a high win-rate/low-reward strategy can lose money (large occasional losses). Promotion decisions use profit factor and risk-adjusted return metrics, not win rate in isolation.
- **Backtest-vs-live divergence tracking:** once live, the strategy's actual performance is continuously compared against its backtest expectation. Significant divergence is this system's early warning that a strategy is overfit or that the market regime has shifted — this feeds directly into the daily re-evaluation step that can trigger strategy regeneration (Section 2.5).

### 7.6 Promotion gate summary
A strategy is only eligible for auto-promotion to live trading if it clears **all** configured thresholds above simultaneously (not a weighted average that lets one bad metric hide behind good ones on the others). Thresholds themselves should be stored as configurable policy (a `strategy_evaluation_policy` concept), not hardcoded, so they can be tuned over time without a code change.

---

## 8. Database Schema (PostgreSQL + TimescaleDB)

This is a logical schema — column types and full constraints should be finalized during implementation, but table structure and relationships should follow this shape.

### 8.1 Core entity tables

```
users
  id, email, password_hash / auth_provider_id, created_at, ...

providers
  id, user_id, name (e.g. "Binance", "Zerodha"), type (enum), status (active/inactive),
  trading_enabled (bool, global on/off for this provider), created_at

provider_credentials
  id, provider_id, credential_json (plaintext for now — see Section 9.1), created_at, updated_at

provider_schedule
  id, provider_id, active_days (e.g. Mon-Fri), active_start_time, active_end_time, timezone

provider_balance_snapshots
  id, provider_id, tradable_balance, currency, synced_at
  -- append-only log; latest row per provider = current balance
```

### 8.2 Market data tables

```
market_types
  id, provider_id, name (e.g. "spot", "futures", "options")

tickers
  id, provider_id, market_type_id, symbol, interval, status (active/inactive/onboarding),
  onboarding_stage (enum: fetching_history / generating_strategy / backtesting / evaluating / ready / failed),
  created_at

ohlcv_data   -- TimescaleDB hypertable, partitioned by time
  ticker_id, timestamp, open, high, low, close, volume
  -- primary access pattern: range queries by ticker_id + timestamp

data_quality_flags
  id, ticker_id, flag_type (gap / duplicate / spike / tz_mismatch), detail_json,
  detected_at, resolved_at (nullable)
```

### 8.3 Strategy tables

```
strategies
  id, ticker_id, version (int, incrementing per ticker), status (draft/backtested/evaluated/live/retired),
  execution_mode (enum: mode_a_rules / mode_b_ai_live),
  parameters_json, generated_by (llm_model identifier), created_at

backtest_results
  id, strategy_id, sharpe, sortino, calmar, max_drawdown, drawdown_duration,
  profit_factor, trade_count, monte_carlo_summary_json, regime_breakdown_json,
  passed_evaluation_gate (bool), evaluated_at

strategy_evaluation_policy
  id, name, min_sharpe, max_drawdown_pct, min_profit_factor, min_trade_count,
  max_parameter_count, is_active
  -- configurable thresholds referenced by the evaluation module, not hardcoded

live_vs_backtest_divergence
  id, strategy_id, measured_at, backtest_expected_metric, live_actual_metric,
  divergence_pct, flagged (bool)
```

### 8.4 Allocation, risk, and execution tables

```
allocation_snapshots
  id, provider_id, ticker_id, allocated_capital, allocation_pct, computed_at
  -- one row per ticker per rebalance run; latest per ticker = current ceiling

risk_limits
  id, provider_id, daily_loss_limit_pct (default 2.0), max_concurrent_positions_per_ticker (default 1),
  reset_boundary (config: exchange_day / utc_midnight)

daily_loss_tracking
  id, provider_id, tracking_date, capital_under_management_base, realized_pl, unrealized_pl,
  limit_breached (bool), breached_at (nullable)

positions
  id, ticker_id, strategy_id, status (open/closed), entry_price, quantity, opened_at, closed_at,
  is_probation (bool)

orders
  id, position_id, provider_order_id, side (buy/sell), quantity, price, order_type,
  status (pending/filled/partial/rejected/cancelled), submitted_at, filled_at
```

### 8.5 Reconciliation, alerts, and cost tracking tables

```
reconciliation_reports
  id, provider_id, run_at, mismatches_found (int), detail_json, status (clean/mismatch)

alerts
  id, severity (critical/warning/info), category, message, related_entity_type, related_entity_id,
  created_at, acknowledged_at (nullable)

llm_cost_log
  id, ticker_id (nullable), strategy_id (nullable), purpose (strategy_generation / re_evaluation / live_decision),
  llm_provider (direct_api / bedrock), model, input_tokens, output_tokens, cost_usd, called_at

ai_lessons_learned
  id, source_strategy_id, source_divergence_id (nullable, FK to live_vs_backtest_divergence),
  ticker_id, market_type, strategy_type, regime_tags (array/json),
  outcome (success/failure), summary_text (the distilled, generalized lesson — not raw logs),
  created_at
  -- retrieved via tag matching (ticker_id / market_type / strategy_type / regime_tags) when
  -- generating or re-evaluating a strategy — see Section 4.11.2
```

---

## 9. Configuration & Extensibility

### 9.1 Secrets & configuration management — everything via `.env`, nothing hardcoded

**Hard rule for implementation: no credential, connection string, port, or secret is ever hardcoded in source code, at any layer.** Every one of these is read from environment variables, loaded from a `.env` file (git-ignored) via a `.env.example` template that documents every required variable with a placeholder value. This applies uniformly to database config, Redis config, JWT secret, and third-party API keys — not just provider trading credentials.

**Two distinct categories of secrets in this system — don't conflate them:**

1. **Infrastructure/app-level secrets** (DB connection, Redis connection, JWT signing secret, LLM API keys) — these belong in `.env`, read once at application startup via a config module (e.g., NestJS `ConfigModule`), and injected wherever needed. These never touch the database.
2. **User-provided provider trading credentials** (Binance API key/secret, Kite API key/secret, etc.) — these are added by the user at runtime through the app UI, so they can't live in a static `.env` file. They're stored in the `provider_credentials` table.
   - **Current phase (local, single-user):** stored as plaintext in `provider_credentials`. This is an explicit, deliberate short-term decision, acceptable only because the system runs locally and is not exposed externally.
   - **Design requirement regardless of phase:** implement access to these behind a `SecretsProvider` interface with a `PlaintextSecretsProvider` implementation now. When the system moves beyond local use, this becomes a swap to a `KmsSecretsProvider` (or equivalent, e.g., AWS KMS/HashiCorp Vault-backed) implementation — not a rewrite. Every place that reads a provider credential goes through this interface, never reads the `provider_credentials` table directly.

**Required `.env` variables (see Section 13 for the full infra setup and matching `.env.example`):** database connection, Redis connection, JWT secret and expiry, LLM provider selection + API keys, per-provider webhook/callback URLs if applicable, application port(s), Node environment (development/production).

### 9.2 LLM provider configurability
- The system must support both **direct Anthropic API** and **Amazon Bedrock** as LLM backends, selectable via configuration (not hardcoded).
- Implement behind an `LLMProvider` interface (e.g., `generateCompletion(prompt, options) -> response`) with `DirectAnthropicProvider` and `BedrockProvider` implementations. All strategy generation, re-evaluation, and Mode B live-decision calls go through this interface.
- Every call through this interface is logged via the LLM Cost Tracking Module (Section 4.10), regardless of which backend served it.

### 9.3 Context window / token efficiency
- Per Section 2.7: the LLM is never given raw historical price data directly. All strategy generation and evaluation prompts should be built from **pre-computed summary statistics** (indicator values, backtest metrics, regime summaries), keeping prompts small and token usage low and predictable regardless of how much historical data underlies the analysis.
- Numerical/statistical processing (feature engineering, backtesting, Monte Carlo simulation) is implemented with vectorized/classical methods (pandas/numpy-equivalent, or a proper backtesting library) — never routed through an LLM call.

---

## 10. Background Job Summary

All of the following run as scheduled or event-triggered background workers via a job queue (e.g., BullMQ), explicitly decoupled from the live request/execution path:

| Job | Trigger | Purpose |
|---|---|---|
| Provider balance sync | Scheduled (frequent, e.g. every few minutes during active hours) | Keep tradable balance current for risk gate & allocation |
| Reconciliation | Scheduled (e.g., hourly or daily) | Compare recorded positions/orders vs. provider actual state; report-only, non-blocking |
| Historical backfill | On new ticker added | Pull missing historical data before strategy generation |
| Strategy generation | On new ticker onboarding | Full pipeline: data → strategy → rules → backtest → evaluate → (if passed) promote at probation size |
| Strategy re-evaluation | Scheduled (e.g., daily) | Check live-vs-backtest divergence for every live strategy; trigger regeneration if flagged |
| Lesson extraction | On strategy retirement/replacement (triggered from strategy re-evaluation) | Summarize the outcome into a structured `ai_lessons_learned` entry (Section 4.11) |
| Allocation rebalance | Scheduled (daily, alongside re-evaluation) | Recompute per-ticker allocation ceilings |
| Data quality monitor | On ingestion + scheduled | Detect gaps/duplicates/anomalies; raise flags |
| Alert digest (warning-tier) | Scheduled (e.g., every few hours) | Batch non-critical alerts rather than spamming |

---

## 11. Build Order / Implementation Phases

Recommended sequence to get to a working system incrementally, front-loading the pieces everything else depends on:

**Phase 1 — Foundation**
- Create `PROGRESS.md` and `CONVENTIONS.md` at the repo root (Section 0.4) as the very first commit — before any feature code — so continuity tracking exists from day one, not bolted on later.
- Auth module, basic multi-page app shell, dark/light mode.
- Provider Management: add Binance provider (Section 4.2.1 — official SDK + Binance's LLM-context docs), credential storage (plaintext), balance sync, provider on/off switch.
- Market Data: market types, ticker add/list, historical backfill, TimescaleDB storage.

**Phase 2 — Risk & Execution skeleton (before any AI)**
- Risk Limits + daily loss tracking tables and the enforcement gate itself, wired to manual/test orders first.
- Positions/orders tables, basic order placement against Binance.
- Reconciliation worker (basic version).

**Phase 3 — Strategy generation & evaluation**
- Strategy Evaluation Module with configurable policy thresholds (Section 7), tested against manually-authored or simple rule-based strategies first, independent of any LLM.
- LLM provider interface (direct API + Bedrock), LLM cost logging.
- `ai_lessons_learned` table (Section 8.5) and basic tag-based retrieval, even with an empty/near-empty table initially — the schema and retrieval path should exist before there's much data to retrieve, so it's exercised from the first generated strategy onward rather than retrofitted later.
- Strategy generation pipeline (Mode A only initially): LLM proposes logic (informed by any relevant retrieved lessons, Section 4.11.2) → evaluation module scores it → promotion at probation size if it passes.

**Phase 4 — Allocation & full automation**
- Allocation/rebalance module (equal-weight + performance-multiplier policy).
- Daily re-evaluation + divergence tracking + auto-regeneration on failure.
- Lesson extraction wired into the re-evaluation/replacement flow (Section 4.11.1) — every retirement or replacement from this point on produces a lesson, not just a log entry.
- Alerting module with full severity tiering.

**Phase 5 — Mode B and polish**
- Mode B (AI live-decision execution) for explicitly flagged tickers.
- Data quality monitoring, dashboard P/L views (by ticker, by provider, overall).
- Additional providers (Zerodha/Kite) using the now-proven Binance integration as the template.

---

## 12. Open Configuration Values (to be set explicitly, not left implicit)

These are called out throughout the document but worth collecting in one place — implementers should treat these as named, documented config values from day one, not magic numbers buried in code:

- Daily loss limit percentage (default 2%, per provider).
- Daily loss reset boundary (exchange trading day vs. UTC midnight, per provider).
- Max concurrent positions per ticker (default 1).
- Probation sizing percentage and duration (e.g., 25% size for first N trades or M days).
- Strategy evaluation thresholds (min Sharpe, max drawdown %, min profit factor, min trade count, max parameter count).
- Allocation diversification cap (max % of pool any single ticker can receive).
- Balance sync frequency.
- Reconciliation run frequency.
- Strategy re-evaluation frequency.
- LLM budget cap per ticker/day (optional).
- Lesson retrieval scope for strategy generation/re-evaluation (which tags must match — ticker-only vs. ticker-or-similar-asset-type vs. regime-inclusive — per Section 4.11.2).

---

## 13. Infrastructure Setup (Docker) — Tools Only

**This section covers infra tools only — Postgres/TimescaleDB and Redis — set up now, independently of any application code.** No backend or frontend code is built or run as part of this. Section 14 separately documents how the backend project (once it's actually built, whether by hand or by an LLM working from this spec) should containerize and manage itself — that's guidance for later, not something set up in this phase.

**Project structure: single repo (monorepo), two decoupled app folders inside it.** This is one project you clone/open once — `algo-trading-platform/` — containing `apps/frontend/` (AstroJS, set up manually with its own design tooling) and `apps/backend/` (NestJS API + worker), each with its own `package.json` and dependency tree. They share no code or build step and communicate purely over HTTP/WebSocket, but they live in one repository rather than two, so there's a single place to work from and a single place to hand to an LLM for implementation. Recommended top-level layout:

```
algo-trading-platform/
├── apps/
│   ├── frontend/        (AstroJS)
│   └── backend/         (NestJS — API + worker, TypeORM migrations)
├── infra/
│   └── docker-compose.yml         (Postgres/TimescaleDB, Redis — tools only)
├── .env                  (infra config, git-ignored)
└── README.md
```

Don't merge `apps/frontend` and `apps/backend` into one `package.json` — Astro and NestJS have incompatible build tooling and dependency graphs, and merging them would force a full rebuild/redeploy of one app whenever the other changes for no benefit. One repo, separate app folders is the standard pattern for this (same idea as Nx/Turborepo/npm workspaces monorepos) and is what's assumed for the rest of this section.

### 13.0 Step one: create the folder structure

Before anything else — before installing any dependency, before writing any code — create the directories above:

```bash
mkdir -p algo-trading-platform/apps/frontend
mkdir -p algo-trading-platform/apps/backend
mkdir -p algo-trading-platform/infra
cd algo-trading-platform
git init
```

Then:
- Put `docker-compose.yml` and `.env` (copied from `.env.example`) inside `infra/`.
- Set up the AstroJS project inside `apps/frontend/` following the exact scaffolding steps in Section 15.1 (project creation, design skills, MCP config) — do this before writing any frontend code, same as the folder structure itself.
- Leave `apps/backend/` empty until it's actually built (by hand or by an LLM working from this spec, per Section 14).
- Add a root `.gitignore` covering at minimum: `.env`, `node_modules/`, `dist/`, `apps/*/node_modules/`, `apps/*/dist/`.

### 13.1 Tools to run

| Tool | Image | Purpose | Notes |
|---|---|---|---|
| `postgres` | `timescale/timescaledb:latest-pg16` | Primary datastore (relational + time-series via TimescaleDB extension) | New, separate instance — see 13.2. |
| `redis` | `redis:7-alpine` | Cache, pub/sub, job queue backing store | Replaces the standalone container you already started. |
| `redis-commander` *(optional, dev convenience)* | `rediscommander/redis-commander` | GUI for inspecting Redis | Not required; useful while developing. |

Postgres/TimescaleDB is managed via **DBeaver** (already installed) rather than a containerized pgAdmin — no need to run two DB GUIs for the same job, so pgAdmin is deliberately left out of this stack.

### 13.2 Why the Postgres container needs to be a different instance

TimescaleDB is a Postgres **extension**, but it must be compiled into the Postgres image — a plain `postgres:latest` image cannot run `CREATE EXTENSION timescaledb;` successfully. Rather than touching your existing Postgres container (which other projects depend on), this project gets its **own separate container and volume**, on a different host port:

- Host `localhost`, port `5433` (deliberately not `5432`, which stays free for your existing instance).
- User/password `postgres`/`postgres`, database `postgres`, schema `Algo_Trading`.
- If you ever want to migrate existing data from another Postgres instance into this one, `pg_dump`/`pg_restore` works fine since the wire protocol and SQL are fully Postgres-compatible — only the container image differs.
- The `timescale/timescaledb` image auto-enables the `timescaledb` extension on the default database the first time it boots — no manual step or init script needed for that part. The `Algo_Trading` schema itself is created by the backend project's first migration, not by the infra layer — see Section 14.1.

### 13.3 `docker-compose.yml`

See the accompanying `docker-compose.yml` file (lives in `infra/`, per the layout above). It starts only `postgres`, `redis`, and optionally `redis-commander` — no application containers.

### 13.4 `.env.example`

See the accompanying `.env.example` file — it covers only what these infra tools need (ports, DB credentials). Copy it to `infra/.env` before running `docker compose up`; `.env` must be git-ignored and never committed. Application-level secrets (JWT signing key, LLM API keys, provider credentials) are **not** part of this file — those belong to the backend project once it exists, per Section 9.

### 13.5 Running it

```bash
cd infra

# start the infra tools
docker compose up -d

# check logs
docker compose logs -f postgres redis

# stop everything
docker compose down

# stop and wipe volumes (fresh start — destroys DB data)
docker compose down -v
```

---

## 14. Guidance for the Backend Project (for whoever builds it — human or LLM)

This section is **not something to set up now** — it's context to carry forward into the backend project itself, whenever it's built (including by an LLM working from this spec). It assumes the infra tools in Section 13 are already running.

### 14.1 Schema management: migrations only, never manual SQL

**Hard rule: tables are created and modified exclusively through versioned migration files, never by hand-running `.sql` scripts against the database (in any environment, including local dev).** This keeps the schema in Section 8 as living, version-controlled code rather than something that silently drifts from what the application expects.

- **Tooling:** TypeORM migrations (matches the NestJS backend called for elsewhere in this spec). Each schema change (new table, new column, new index, etc.) is a generated migration file checked into the backend repo under `src/migrations/`, with a matching `up()` and `down()` so changes are reversible.
- **The very first migration is responsible for creating the `Algo_Trading` schema itself** (`CREATE SCHEMA IF NOT EXISTS "Algo_Trading";`), before any table-creation migrations run. The TimescaleDB extension is already auto-enabled by the Postgres image on first boot (see Section 13.2), so the migration doesn't need to handle that — only the schema. This replaces the separate Docker init-script approach considered earlier: schema setup goes through the same migration mechanism as every other schema change, not a side mechanism.
- **Workflow for any schema change:** update the relevant TypeORM entity class(es) in code → run `migration:generate` to diff entities against the current DB state and produce a migration file → review it (auto-diff is usually right but not infallible) → commit it.
- **TimescaleDB hypertables need one manual line per time-series table.** TypeORM's auto-generated migrations create standard Postgres tables; converting `ohlcv_data` (and any other time-series table) into a hypertable requires manually adding `SELECT create_hypertable('ohlcv_data', 'timestamp');` into that migration's `up()` (and corresponding drop logic in `down()` if needed). This is the one place raw SQL legitimately belongs — inside a migration file, never as a standalone script.
- **Never edit a migration that's already been run** against any persistent database. Fix mistakes forward with a new migration, same as application code.
- **Migrations must run automatically before the backend or worker container starts serving/processing anything** — e.g., via a startup entrypoint that runs the migration command first, then starts the actual server/worker process — on both initial start and every restart. This keeps the schema always in sync with the code about to run against it, without a manual step.

### 14.2 Backend containerization (once the backend project exists)

- `backend` (API) and `worker` (background job processor from Section 10) should run as **separate containers from the same image**, differing only in start command — this is what physically guarantees background jobs (reconciliation, strategy generation, re-evaluation) never block the live order-execution path (Section 3's architectural rule), rather than relying on careful async code within a single process.
- Both connect to the infra tools from Section 13 using the connection details already established (Postgres on `5433`, Redis on `55000`).
- All application-level secrets (JWT signing key, LLM provider API keys, etc.) go in the backend project's own `.env`, following the same "nothing hardcoded" rule from Section 9.1 — this file is separate from the infra `.env` in Section 13.4.

---

## 15. Frontend Scaffolding & Design Principles

This section governs how `apps/frontend` gets set up — whether done by hand now or later by an LLM working from this spec. Follow these steps **in order**, from inside `apps/` (so the project lands at `apps/frontend`):

### 15.1 Scaffolding steps

1. **Create the Astro project:**
   ```bash
   npm create astro@latest ./frontend
   ```
   Run this from inside `apps/`, so the result is `apps/frontend/`.

2. **Add the Tailwind design skill:**
   ```bash
   npx skills add https://github.com/lombiq/tailwind-agent-skills --skill tailwind-4-docs
   ```

3. **Add the web design guidelines skill:**
   ```bash
   npx skills add https://github.com/vercel-labs/agent-skills --skill web-design-guidelines
   ```

4. **Configure the Astro docs MCP server** so any LLM working on this project has direct access to current Astro documentation rather than relying on training data (Astro's APIs move fast enough that this matters). Add to the relevant MCP config:
   ```json
   {
     "mcpServers": {
       "astro-docs": {
         "serverUrl": "https://mcp.docs.astro.build/mcp"
       }
     }
   }
   ```

**Why these matter as a documented sequence, not just a one-time setup note:** if a different LLM (or you, months later) picks up frontend work, these four steps — and the resulting skills/MCP config already present in the repo — mean it inherits the same design system and up-to-date framework knowledge you're setting up now, rather than reinventing conventions or working from stale Astro knowledge.

### 15.2 Design principles to follow

With the Tailwind and web-design-guideline skills installed per 15.1, treat those as the authoritative source for spacing, typography, component conventions, and layout principles — this spec intentionally doesn't duplicate that guidance here to avoid it drifting out of sync with the skills themselves. Any LLM building `apps/frontend` should read those installed skills before writing UI code, the same way it should read this spec before writing backend code.

---

## 16. Frontend Functional Requirements: Main Dashboard

This section captures a functional requirement that didn't have an obvious home earlier in this spec: what the **main page** of the app actually needs to show. It's called out separately because it drives real-time data flow requirements on the frontend (WebSocket subscriptions, live-updating chart state) that are easy to miss if buried inside a generic "dashboard" mention.

### 16.1 Main page — selected ticker view

- A **ticker selector** lets the user pick one of their managed tickers.
- For the selected ticker, the main page shows:
  - A **real-time price chart** (candlestick or line, sourced from the live OHLCV data described in Section 4.3 — pushed to the frontend via the WebSocket path described in Section 3's architecture diagram, not polled).
  - The **current strategy's suggestion/action** for that ticker — i.e., what the live strategy (Mode A rules engine or Mode B AI decision, per Section 2.6) currently recommends or has most recently acted on (e.g., "holding," "entered long at X," "exit signal triggered"), sourced from the `strategies`/`positions` tables in Section 8.

### 16.2 Sidebar — live trades across all tickers

- A persistent sidebar (visible regardless of which ticker is selected in the main view) lists **all currently open/live trades across every managed ticker and provider** — not just the one selected in the main chart.
- Each entry should show enough at a glance to be useful without clicking in: ticker, provider, side (long/short), entry price, current unrealized P/L, and how long the position has been open.
- This should update live via the same WebSocket path as the main chart, not require a page refresh.

### 16.3 Implication for the frontend's data layer

Because both the main chart and the sidebar need live-updating data simultaneously (one ticker's detail + all tickers' open trades), the frontend's WebSocket client should maintain a shared, page-level subscription/state layer rather than each component independently polling or subscribing — avoids redundant connections and keeps the two views consistent with each other.

---

*End of specification.*