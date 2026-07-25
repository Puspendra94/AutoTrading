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
| `positions:update` | worker (P3c) | backend | `{ tickerId }` (backend re-reads open positions) |
| `strategy:regenerate` | supervisor (P3a) | backend gen | `{ tickerId, strategyId, reason, triggeredBy }` |

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

### Phase 2 — Async job dispatch (decouple worker from backend)  ✅ (this change)
- **Refinement of the original "move bodies to worker" step.** Four of the six jobs
  (`balance-sync`, `reconciliation`, `allocation-rebalance`, `strategy-reevaluation`) depend
  on the Binance provider adapter, credential handling, and the LLM — i.e. the exact code
  Phase 3 ports. Re-implementing them in Python now would pull Phase 3's risk forward, so
  the **bodies stay in the backend** for now and move in Phase 3 with their dependencies.
- What changes instead: the **trigger** becomes async. The worker publishes a job name to
  Redis `jobs:trigger` (instead of a synchronous `POST /internal/jobs/*`); the backend's
  `InternalJobsRedisConsumer` picks it up and runs the same proven `InternalJobsService`
  body. The worker no longer blocks on — or fails because of — the backend, so the two
  deploy and restart independently.
- Gated by `JOB_DISPATCH` (worker): `http` (default, legacy) or `redis` (decoupled). The
  backend always listens on `jobs:trigger`, so cutover is a worker-only flag flip. The
  guarded HTTP endpoints remain for manual/debug triggers.
- Delivery is fire-and-forget pub/sub: if the backend is down, that periodic tick is skipped
  and runs next cadence (fine for 5-min/hourly jobs). Upgrade to Redis Streams later if
  at-least-once delivery is needed.
- **Completes problem 2** (independent deploy + async comms).

### Phase 3 — Strategy analysis, guardrails, execution, and the Strategy Supervisor → worker
Split into sub-phases by risk (money/AI logic last):

- **3a — Strategy Supervisor (deterministic trigger)  ✅ (this change).** No AI, no
  execution: the worker computes live guardrail metrics (PF divergence, consecutive losses,
  drawdown, win-rate decay) per LIVE strategy and, on a trip, publishes `strategy:regenerate`.
  The backend's `StrategyRegenerateConsumer` runs the existing, audited
  `generateStrategyForTicker` (one generation implementation, unchanged). Purely additive and
  flag-gated (`SUPERVISOR_ENABLED`, default off); when on, it is the responsive replacement
  for the daily `strategy_reevaluation` trigger. The decision math is pure functions with unit
  tests (`tests/test_supervisor.py`).
- **3b — LLM strategy generation → worker.** Ported faithfully in layers behind a parity
  harness; the supervisor eventually generates in-process instead of publishing to the backend.
  - **3b-1 — LLM layer  ✅ (this change).** Faithful Python port of `LlmChainBuilder` +
    `LlmService` + the zod schemas (`worker/llm/`): same `LLM_MODELS` fallback chain, same
    per-provider build (`langchain-anthropic` / `langchain-aws` / `langchain-deepseek`), same
    structured-output method selection (DeepSeek → `json_mode`), same `-fallback` synthetic
    path, same pricing table, and `llm_cost_log` writes. Deterministic parts unit-tested
    (`tests/test_llm.py`, 8 cases). No caller wired yet — it's the foundation 3b-2/3b-3 use.
  - **3b-2 — evaluator/backtest engine  ✅ (this change).** Faithful port of
    `strategy-evaluator.service.ts` → `worker/strategy/evaluator.py` (walk-forward split,
    Sharpe/Sortino/Calmar, drawdown, profit factor, regime breakdown, parameter count, gate),
    including a `to_fixed` that matches JS `Number(x.toFixed(n))` on the exact double.
    Validated by a **parity harness** (`tools/parity_evaluator.py` +
    `apps/backend/tools/parity_evaluator_runner.ts`): identical candles through the real TS
    service and the Python port — every deterministic field matches across 4 scenarios × 2
    policies (gate=False and gate=True). Monte Carlo fields are RNG-driven in both and
    excluded; the gate does not depend on them. Golden values from the TS run are pinned in
    `tests/test_evaluator.py` so drift fails CI without needing Node.
  - **3b-3 — generation orchestration + wiring  ✅ (this change).** Ported
    `generateStrategyForTicker` → `worker/strategy/generator.py`: exact prompt build with
    lessons, gate-before-save 3-attempt retry loop, persist strategy + backtest, retire the
    previous LIVE (record its lesson first, LLM-summarized with the same template fallback),
    promote, and the data-quality-flag refusal. DB work is isolated behind a `GeneratorStore`
    protocol (`pg_store.py` for production) so the control flow is unit-tested with a fake
    store + fake LLM against the real evaluator (`tests/test_generator.py`, 7 cases). The
    Supervisor gains `SUPERVISOR_GENERATE_INLINE` (default off): off = publish
    `strategy:regenerate` (current behavior); on = run generation in-process. At cutover, flip
    it on and retire the backend `StrategyRegenerateConsumer`.
- **3c — Risk gate + execution → worker.** Split by risk (real orders last):
  - **3c-1 — Risk gate  ✅ (this change).** Faithful port of `risk-gate.service.ts` →
    `worker/execution/risk_gate.py`: kill switch, trading-enabled, provider schedule,
    daily-loss breach (mark + critical alert + flatten + halt), max concurrent positions,
    allocation ceiling, and probation sizing. DB reads/side-effects behind a `RiskGateStore`
    protocol (`pg_store.py`). Unit-tested against the SAME scenarios as
    `risk-gate.service.spec.ts` plus the sizing/probation/breach math
    (`tests/test_risk_gate.py`, 10 cases). Places NO orders and is not wired to any caller —
    `flatten_all_positions` is a loud no-op until 3c-2.
  - **3c-2 — Execution engine  ✅ (this change).** Faithful port of `execution.service.ts` →
    `worker/execution/execution.py`: executeTradeSignal (gate → live-or-simulated fill →
    position+order write), closePosition (P/L), flattenAllPositionsForProvider, and
    enforceHardExits. A real order is placed ONLY in `live` + `binance` + creds
    (`BinanceOrderPlacer`, `binance-connector`); otherwise a simulated `SIM_*` fill at the real
    streamed price. `record_api_failure` trips the kill switch at the threshold. Order placement
    is behind an `OrderPlacer` and DB behind `ExecutionStore` (`execution_pg_store.py`), so the
    whole flow is unit-tested on the simulated + live-branch paths with fakes — never touching
    Binance (`tests/test_execution.py`, 11 cases). The risk gate's `flatten_all_positions` now
    delegates here when wired. **Not connected to the live tick loop yet.**
  - **3c-3 — Live-loop integration  ✅ (this change).** Ported `evaluateLiveSignal` (Mode A
    deterministic EMA rules + Mode B AI decision) → `worker/execution/signals.py`, and the
    per-tick loop → `worker/execution/live_executor.py`: on every tick mark-price + hard exits,
    on candle close evaluate → BUY opens (via the gate+engine), SELL closes the matching
    position. `factory.build_live_executor` wires the risk gate's flatten to the engine and
    publishes `positions:update`. Wired into `live_stream` behind `WORKER_OWNS_EXECUTION`
    (default off); the backend gains `LIVE_EXECUTION_SOURCE` (default `backend`) so when the
    worker owns execution the backend stops executing and just forwards `positions:update`.
    Mode A signals + the tick/execute dispatch are unit-tested (`tests/test_signals.py`,
    `tests/test_live_executor.py`, 12 cases); Mode B and live orders need a testnet run.
  - **Cutover:** set `WORKER_OWNS_EXECUTION=true` (worker) + `LIVE_EXECUTION_SOURCE=worker`
    (backend) together — never one alone (double orders / none). Validate on Binance testnet
    (provider `useTestnet=true`, `trading_mode=live`) before mainnet.
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
