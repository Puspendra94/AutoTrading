# Service-Separation Migration Plan

Goal: make the three services (backend API, frontend, Python worker) independently
deployable and independently resilient, with **all data + AI operations owned by the
Python worker** and **async, Redis-based communication** between processes. UI/UX is
unchanged throughout.

## Why (the problems this solves)

1. **Log noise** — data-ingestion insert logs share the backend's log stream, drowning
   out API logs. Moving ingestion to the worker separates the two streams at the process
   boundary.
2. **Independent deploy** — today the worker calls the backend over HTTP
   (`/internal/jobs/*`), so it can't run without the backend. Removing that dependency lets
   all three deploy/restart in any order.
3. **Ingestion resilience** — live streaming runs in the backend, so a backend crash stops
   data ingestion and leaves a large gap to backfill on restart. The worker must own
   ingestion so candles keep landing regardless of backend state.
4. **True parallelism** — heavy CPU work (strategy analysis, guardrails, backtests) blocks
   the Node event loop. Python worker processes/pools give real multi-core parallelism, and
   each pipeline becomes an independently scalable unit.

> **Note on parallelism:** Python has a GIL too — raw threads don't parallelize CPU work in
> either language. Real parallelism comes from **multiple processes** (Python
> `multiprocessing`/process pools, or multiple worker containers). The win is the process
> topology, not the language.

## Target architecture

```
┌──────────────┐     WS /socket.io (live UI)      ┌───────────────────────┐
│   frontend   │ ◀──────────────────────────────  │       backend         │
│  (AstroJS)   │ ──── HTTP /api (reads DB) ──────▶ │  (NestJS: API + WS)   │
└──────────────┘                                   └───────────┬───────────┘
                                                                │ subscribes
                                            Redis pub/sub  ◀─────┘  (fan-out to browsers)
                                                  ▲
                                                  │ publishes events
┌──────────────────────────────────────────────────────────────────────────┐
│                          worker-py (owns data + AI)                        │
│  ┌───────────┐ ┌───────────┐ ┌───────────────┐ ┌────────────────────────┐ │
│  │ historical│ │   live    │ │  strategy gen │ │  Strategy Supervisor   │ │
│  │  backfill │ │ ingestion │ │  + execution  │ │ (deterministic memory) │ │
│  └───────────┘ └───────────┘ └───────────────┘ └────────────────────────┘ │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                        │ TypeORM/asyncpg
                                        ▼
                             ┌───────────────────────┐
                             │  TimescaleDB + Redis   │
                             └───────────────────────┘
```

The backend becomes a thin **API + WebSocket + DB-read** layer with no trading logic. The
worker owns every pipeline and communicates outward by **publishing events to Redis**; the
backend subscribes and fans them out to browsers over Socket.IO (the exact pattern the
alert path already uses).

## Redis event contract

One channel per event type; JSON payloads. Pub/sub for live fan-out; migrate hot/durable
streams to Redis Streams later if at-least-once delivery is needed.

| Channel            | Publisher | Consumer | Payload |
| ------------------ | --------- | -------- | ------- |
| `market:tick`      | worker    | backend  | `{ tickerId, symbol, interval, openTime, closeTime, open, high, low, close, volume, isFinal }` |
| `alerts:created`   | backend/worker | backend | existing alert JSON (already live) |
| `positions:update` | worker (P3) | backend | `{ tickerId }` (backend re-reads open positions) |
| `strategy:regenerate` | supervisor (P3) | worker gen | `{ tickerId, reason, triggeredBy }` |

## Phases (shipped in sequence)

### Phase 1 — Live ingestion → worker  ✅ (this change)
- Worker owns the Binance kline WebSocket, upserts final candles to `ohlcv_data`, and
  publishes every tick to `market:tick`.
- Backend `MarketStreamService` switches from *owning the socket* to *consuming
  `market:tick`* behind `LIVE_STREAM_SOURCE` (`internal` = legacy in-process, `redis` =
  worker-driven). All downstream logic (chart broadcast, mark-price, hard exits,
  evaluate+execute) is untouched — only the tick *source* changes.
- Cutover is a single flag flip; `internal` remains the default so nothing changes until
  you opt in. Never run worker streaming and backend `internal` streaming at once (double
  ingestion).
- **Solves problems 1, 2 (partial), 3.**

### Phase 2 — Job execution → worker
- Move the six `/internal/jobs/*` bodies (`InternalJobsService`) into the worker so it
  *executes*, not just *triggers*. Remove the HTTP round-trip and the backend↔worker
  runtime dependency.
- **Completes problem 2.**

### Phase 3 — Strategy analysis, guardrails, execution, and the Strategy Supervisor → worker
- Port strategy generation (LLM), evaluation, the risk gate, and order execution into the
  worker, run as parallel pipelines (process pool for CPU-heavy stages).
- Add the **Strategy Supervisor** pipeline (below).
- **Solves problem 4.**

## Strategy Supervisor (Phase 3) — deterministic memory + gated regeneration

Two tiers of "knowledge", so AI is a rare consumer, never the builder:

- **Tier 1 — deterministic (no AI), continuous.** From live candles + executed trades,
  maintain rolling `strategy_performance` (win rate, drawdown, sharpe, avg trade return)
  and write `live_vs_backtest_divergence` rows. Pure computation.
- **Tier 2 — AI generation (paid, rare).** Fires only when a Tier-1 guardrail trips.

Loop:
1. **Observe** live candle + fill events from Redis.
2. **Update knowledge (no AI)** — rolling metrics + divergence rows.
3. **Evaluate trigger conditions (guardrails)** — tunable thresholds:
   - divergence % over limit
   - N consecutive losing trades
   - max-drawdown breach
   - win-rate decay below floor
   - regime shift
4. **Trigger** — emit `strategy:regenerate` (deterministic; no AI yet).
5. **Generate (AI, gated)** — assemble Tier-1 knowledge + prior strategies' parameters and
   failure records (`retrieveRelevantLessons` + `formatLessonsForPrompt`) into the prompt;
   generate a challenger.
6. **Promotion gate (champion-challenger)** — challenger backtests, then trades at
   **probation size** (`DEFAULT_PROBATION_SIZE_PCT` / `_TRADES_COUNT`). Promote **only if it
   beats the incumbent** on the success metric over the probation window; else discard — and
   the discard becomes a new deterministic lesson, so the next round is smarter.

Guarantees:
- **No repeated mistakes** — prior failures passed into the prompt as *avoid-these*; a
  novelty/parameter check rejects challengers too close to a strategy that already failed
  here (`parameter_count` on backtest results); `regime_tags` scope lessons to the
  conditions they occurred in.
- **Better than previous** — the promotion gate is a ratchet: nothing is promoted unless it
  demonstrably out-performs the incumbent.

Change from today: `ai-lessons.service.recordLesson()` currently uses the LLM to *write*
the lesson. Flip it to build a **structured, deterministic** record (which condition tripped,
regime, metric deltas, parameter diff). The AI only *reads* those facts at generation time.
