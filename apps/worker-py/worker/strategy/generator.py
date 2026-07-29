"""Strategy generation orchestration — faithful port of
strategy-engine.service.ts::generateStrategyForTicker (Phase 3b-3).

Uses the already-verified LLM layer (worker/llm) and evaluator (worker/strategy/evaluator).
The DB work is isolated behind a small store protocol so the gate-retry-promote control flow
is unit-testable with a fake store + fake LLM against the REAL evaluator; production uses the
asyncpg-backed PgGeneratorStore.

The gate-before-save contract is preserved exactly: a generated strategy is persisted ONLY if
its out-of-sample backtest clears the active policy gate; up to 3 LLM attempts, failed attempts
discarded (never written to `strategies`) though their real LLM cost is still logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional, Protocol

from ..config import config
from ..llm.schemas import StrategyGen, StrategyTemplate
from ..llm.service import LlmPurpose, LlmService
from .dsl.ir import validate_ir
from .evaluator import evaluate_strategy
from .grammar_prompt import build_generation_prompt, build_template_generation_prompt
from .optimizer import optimize

log = logging.getLogger("worker.strategy.generator")

MAX_ATTEMPTS = 3
_INTERVAL_OVERRIDE_RE = re.compile(r"^\d+[mhdw]$", re.IGNORECASE)


def parse_generated_strategy(data: Any) -> Optional[dict]:
    """Build a validated IR dict from the LLM's StrategyGen output; None if the tree is invalid.
    Mirrors strategy-engine.service.ts::parseGeneratedStrategy."""
    d = data.model_dump(exclude_none=True) if hasattr(data, "model_dump") else dict(data)
    ir = {
        "strategyName": d.get("strategyName") or "Generated Strategy",
        "reasoning": d.get("reasoning") or "",
        "entry": d.get("entry"),
        "exit": d.get("exit"),
        "risk": d.get("risk"),
    }
    if d.get("direction"):
        ir["direction"] = d["direction"]
    # A 'both' strategy carries its mirrored short leg; dropping these would silently degrade it to
    # long-only (validate_ir rejects direction='both' without them).
    for key in ("shortEntry", "shortExit"):
        if d.get(key):
            ir[key] = d[key]
    errs = validate_ir(ir)
    if errs:
        log.warning("Generated strategy failed validation: %s", "; ".join(errs[:4]))
        return None
    return ir

# Ticker onboarding stages (mirror OnboardingStage in ticker.entity.ts).
STAGE_GENERATING = "generating_strategy"
STAGE_BACKTESTING = "backtesting"
STAGE_EVALUATING = "evaluating"
STAGE_READY = "ready"
STAGE_FAILED = "failed"


# ---------------------------------------------------------------- pure helpers
def infer_strategy_type(params: Any) -> str:
    """Coarse tag = distinct indicator kinds in the rule tree (mirrors strategyTypeTag).
    Legacy indicatorConfig blobs are auto-translated first."""
    from .dsl.ir import strategy_type_tag
    return strategy_type_tag(params) if isinstance(params, dict) else "unclassified"


def format_lessons_for_prompt(lessons: list[dict]) -> str:
    """Mirror AiLessonsService.formatLessonsForPrompt."""
    if not lessons:
        return "No prior lessons recorded for this ticker/strategy type yet."
    return "\n".join(
        f"{i + 1}. [{str(l['outcome']).upper()}] {l['summaryText']}" for i, l in enumerate(lessons)
    )


def _js_num(x: float) -> str:
    """Render a number the way JS template interpolation does (no trailing '.0')."""
    f = float(x)
    return str(int(f)) if f.is_integer() else repr(f)


# The generation prompt now comes from grammar_prompt.build_generation_prompt (DSL rule tree +
# planner emphasis + market-type direction clause); the legacy indicatorConfig prompt was removed
# when the worker moved to DSL generation (consolidation Phase A).


def _num(v: Any) -> float:
    """Mirror the evaluator's Number() coercion for policy fields (may be str/None -> NaN)."""
    if v is None:
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def looks_like_short(entry: Any) -> bool:
    """True when an entry tree reads as a SHORT setup: enter while an oscillator is OVERBOUGHT
    (rsi/stochastic ABOVE a high level) or price is BELOW a trend line. Used only to catch a
    declared/actual direction MISMATCH — the LLM once emitted a textbook short rule tree while
    omitting direction, so it was executed inverted as a long."""
    hits = {"short": 0, "long": 0}

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        op = node.get("op")
        left, right = node.get("left"), node.get("right")
        if op in ("gt", "lt", "crossAbove", "crossBelow") and isinstance(left, dict):
            kind = (left.get("kind") or "").lower() if left.get("op") == "indicator" else None
            level = right.get("value") if isinstance(right, dict) and right.get("op") == "const" else None
            if kind in ("rsi", "stochastic") and isinstance(level, (int, float)):
                if op in ("gt", "crossAbove") and level >= 55:
                    hits["short"] += 1      # entering while overbought
                elif op in ("lt", "crossBelow") and level <= 45:
                    hits["long"] += 1       # entering while oversold
            if left.get("op") == "price" and isinstance(right, dict) and right.get("op") == "indicator":
                if op in ("lt", "crossBelow"):
                    hits["short"] += 1      # price below its trend line = downtrend filter
                elif op in ("gt", "crossAbove"):
                    hits["long"] += 1
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

    walk(entry)
    return hits["short"] > hits["long"]


def _direction_mismatch(spec: dict, attempt: int, ticker_id: str) -> bool:
    """Discard a proposal whose declared direction contradicts its own rule tree. Trading such a
    strategy would do the exact opposite of what it describes (buying every overbought spike in a
    downtrend), so it is rejected rather than silently inverted."""
    declared = spec.get("direction") or "long"
    if declared == "both":
        # Both legs must read the way they're labelled: `entry` long-shaped, `shortEntry` short-shaped.
        bad = [name for name, tree, want_short in
               (("entry", spec.get("entry"), False), ("shortEntry", spec.get("shortEntry"), True))
               if looks_like_short(tree) != want_short]
        if not bad:
            return False
        log.warning("Strategy attempt %d/%d for ticker %s declares direction=both but %s reads as the "
                    "wrong side — refusing to trade it inverted. Discarding.",
                    attempt, MAX_ATTEMPTS, ticker_id, "/".join(bad))
        return True
    if looks_like_short(spec.get("entry")) == (declared == "short"):
        return False
    log.warning(
        "Strategy attempt %d/%d for ticker %s declares direction=%s but its entry rules read as a %s "
        "setup — refusing to trade it inverted. Discarding.",
        attempt, MAX_ATTEMPTS, ticker_id, declared, "SHORT" if declared == "long" else "LONG",
    )
    return True


def gate_failure_reasons(ev: dict, policy: dict) -> list[str]:
    """Human-readable list of which policy conditions a backtest failed — purely for logging,
    so a discarded attempt says *why* it was rejected instead of a bare 'failed the gate'.
    Mirrors the exact conditions in evaluate_strategy (out-of-sample metrics vs. policy)."""
    reasons: list[str] = []
    if ev["sharpe"] < _num(policy.get("minSharpe")):
        reasons.append(f"sharpe {ev['sharpe']} < minSharpe {_num(policy.get('minSharpe'))}")
    if ev["maxDrawdown"] > _num(policy.get("maxDrawdownPct")):
        reasons.append(f"maxDrawdown {ev['maxDrawdown']}% > maxDrawdownPct {_num(policy.get('maxDrawdownPct'))}%")
    if ev["profitFactor"] < _num(policy.get("minProfitFactor")):
        reasons.append(f"profitFactor {ev['profitFactor']} < minProfitFactor {_num(policy.get('minProfitFactor'))}")
    if ev["tradeCount"] < _num(policy.get("minTradeCount")):
        reasons.append(f"tradeCount {ev['tradeCount']} < minTradeCount {_num(policy.get('minTradeCount'))}")
    if ev["parameterCount"] > _num(policy.get("maxParameterCount")):
        reasons.append(f"parameterCount {ev['parameterCount']} > maxParameterCount {_num(policy.get('maxParameterCount'))}")
    min_avg_hold = policy.get("minAvgHoldBars")
    if min_avg_hold is not None and ev.get("avgHoldBars", 0) < _num(min_avg_hold):
        reasons.append(f"avgHoldBars {ev.get('avgHoldBars', 0)} < minAvgHoldBars {_num(min_avg_hold)}")
    return reasons


def _json(obj: Any) -> str:
    """Compact JSON for prompt embedding, tolerant of DB-native types. Values read straight from
    Postgres arrive as Decimal/datetime, which json.dumps rejects — and this runs inside lesson
    recording, so an encoder error silently cost the platform its learning signal. `default` keeps a
    stray value from failing the whole dump."""
    return json.dumps(obj, separators=(",", ":"), default=str)


def build_lesson_prompt(symbol: str, params: dict, backtest: Optional[dict], reason: str, divergence: Optional[dict]) -> str:
    """Mirror AiLessonsService.recordLesson's summaryPrompt (JSON.stringify -> compact json)."""
    metrics: dict = {}
    if backtest is not None:
        metrics = {
            "sharpe": backtest.get("sharpe"),
            "profitFactor": backtest.get("profitFactor"),
            "maxDrawdown": backtest.get("maxDrawdown"),
        }
    divergence_str = f"{divergence['divergencePct']}% on the tracked metric" if divergence else "n/a"
    return (
        "A trading strategy was just retired. Distill this into ONE short, generalized,\n"
        "reusable lesson (2-3 sentences) for future strategy generation on this or similar tickers — focus on the\n"
        f"pattern/cause, not a trade-by-trade recap. Ticker: {symbol}. Strategy parameters: {_json(params)}. "
        f"Backtest metrics: {_json(metrics)}. Retirement reason: {reason}. "
        f"Live-vs-backtest divergence: {divergence_str}. Respond with plain text only, no JSON."
    )


def build_failure_lesson_prompt(
    symbol: str, params: dict, evaluation: Optional[dict], failing_conditions: list[str], reason: str
) -> str:
    """Mirror AiLessonsService.recordFailureLesson's summaryPrompt (JSON.stringify -> compact json).
    Distills a discarded, gate-failing generation attempt into a reusable 'what to avoid' lesson."""
    conditions = "; ".join(failing_conditions) or "unknown"
    metrics = {
        "sharpe": (evaluation or {}).get("sharpe"),
        "profitFactor": (evaluation or {}).get("profitFactor"),
        "maxDrawdown": (evaluation or {}).get("maxDrawdown"),
        "tradeCount": (evaluation or {}).get("tradeCount"),
    }
    return (
        "A strategy generation attempt just failed the evaluation gate and was discarded (never traded). "
        "Distill this into ONE short, generalized, reusable lesson (2-3 sentences) for the next strategy "
        "generation on this or similar tickers — focus on what to change to clear the gate and improve "
        "profitability, not a trade-by-trade recap. "
        f"Ticker: {symbol}. Attempted parameters: {_json(params)}. "
        f"Out-of-sample backtest metrics: {_json(metrics)}. "
        f"Failing gate conditions: {conditions}. Trigger: {reason}. Respond with plain text only, no JSON."
    )


# ---------------------------------------------------------------- store protocol
class GeneratorStore(Protocol):
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def has_unresolved_blocking_flags(self, ticker_id: str) -> bool: ...
    async def set_onboarding_stage(self, ticker_id: str, stage: str) -> None: ...
    async def load_recent_candles(self, ticker_id: str, limit: int = 2000) -> list[dict]: ...
    async def load_candles_for_interval(self, ticker_id: str, interval: str, limit: int) -> list[dict]: ...
    async def get_latest_strategy_params(self, ticker_id: str) -> Optional[dict]: ...
    async def get_active_policy(self) -> dict: ...
    async def count_strategies(self, ticker_id: str) -> int: ...
    async def insert_strategy(self, *, ticker_id: str, version: int, parameters_json: dict, generated_by: str,
                              eval_interval: Optional[str] = None) -> str: ...
    async def insert_backtest(self, *, strategy_id: str, ev: dict) -> None: ...
    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]: ...
    async def get_backtest_metrics(self, strategy_id: str) -> Optional[dict]: ...
    async def get_latest_divergence(self, strategy_id: str) -> Optional[dict]: ...
    async def retire_live(self, ticker_id: str) -> None: ...
    async def promote_strategy(self, strategy_id: str) -> None: ...
    async def set_ticker_active_ready(self, ticker_id: str) -> None: ...
    async def retrieve_lessons(self, ticker_id: str, strategy_type: Optional[str], limit: int = 5) -> list[dict]: ...
    async def insert_lesson(self, **kwargs: Any) -> None: ...


# ---------------------------------------------------------------- orchestration
class StrategyGenerator:
    def __init__(self, store: GeneratorStore, llm: LlmService) -> None:
        self.store = store
        self.llm = llm

    async def generate(self, ticker_id: str, retirement_reason: str = "new strategy generation cycle",
                       interval_override: Optional[str] = None, skip_gate: bool = False) -> dict:
        ticker = await self.store.get_ticker(ticker_id)
        if not ticker:
            raise ValueError(f"Ticker {ticker_id} not found")

        # Spec 4.7 — refuse if the underlying data has unresolved gap/spike flags.
        if await self.store.has_unresolved_blocking_flags(ticker_id):
            await self.store.set_onboarding_stage(ticker_id, STAGE_FAILED)
            raise ValueError(
                f"Strategy generation refused for ticker {ticker_id}: unresolved data quality flags exist."
            )

        await self.store.set_onboarding_stage(ticker_id, STAGE_GENERATING)

        active_policy = await self.store.get_active_policy()

        # Meta-planner (lazy import: planner imports helpers from this module) picks interval /
        # candleLimit / effective (clamped) gate / signal emphasis from accumulated lessons.
        from .planner import plan_generation
        plan = await plan_generation(self.store, self.llm, getattr(self.store, "pool", None), ticker, active_policy)
        eval_interval = (
            interval_override
            if (interval_override and _INTERVAL_OVERRIDE_RE.match(interval_override))
            else plan["interval"]
        )
        policy = plan["policy"]

        # DSL backtest needs full OHLCV aggregated to the planned interval (not raw close-only 1m).
        candles = await self.store.load_candles_for_interval(ticker_id, eval_interval, plan["candleLimit"])

        prior = await self.store.get_latest_strategy_params(ticker_id)
        strategy_type = infer_strategy_type(prior) if prior else None
        lessons = await self.store.retrieve_lessons(ticker_id, strategy_type)
        lessons_block = format_lessons_for_prompt(lessons)

        latest_price = float(candles[-1]["close"]) if candles else 0
        allow_short = (ticker.get("market_type_name") or "").lower() == "futures"
        param_budget = int(policy.get("maxParameterCount") or 5)
        two_stage = config.two_stage_generation
        prompt_builder = build_template_generation_prompt if two_stage else build_generation_prompt
        prompt = prompt_builder(
            ticker["symbol"], eval_interval, latest_price, param_budget,
            allow_short, plan["signalEmphasis"], lessons_block,
        )

        await self.store.set_onboarding_stage(ticker_id, STAGE_BACKTESTING)

        last_eval = None
        last_params = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            schema = StrategyTemplate if two_stage else StrategyGen
            try:
                llm_res = await self.llm.generate_structured_completion(
                    prompt, schema, schema_name="propose_template" if two_stage else "propose_strategy")
            except Exception as err:  # noqa: BLE001 — a provider hiccup shouldn't abort the run
                # Previously this propagated and killed the whole job on attempt 1, so a transient
                # provider failure cost all remaining attempts.
                log.warning("Strategy attempt %d/%d for ticker %s: LLM produced nothing (%s). Retrying.",
                            attempt, MAX_ATTEMPTS, ticker_id, err)
                continue
            await self.llm.log_cost(
                getattr(self.store, "pool", None),
                ticker_id, None, LlmPurpose.STRATEGY_GENERATION,
                {"model": llm_res["model"], "provider": llm_res.get("provider"),
                 "inputTokens": llm_res["inputTokens"], "outputTokens": llm_res["outputTokens"],
                 "costUsd": llm_res["costUsd"]},
            )

            if two_stage:
                # Stage 1 gave a SHAPE; Stage 2 (optimizer) finds the best parameters walk-forward.
                template = llm_res["data"].model_dump(exclude_none=True)
                if template.get("direction") in ("short", "both") and not allow_short:
                    log.warning("Strategy attempt %d/%d for ticker %s: SHORT/BOTH template on a non-futures market. Discarding.",
                                attempt, MAX_ATTEMPTS, ticker_id)
                    continue
                if _direction_mismatch(template, attempt, ticker_id):
                    continue
                # CPU-bound grid search (hundreds of backtests): run it off the event loop so the
                # shared loop keeps servicing the live kline stream's websocket pongs (else Binance
                # drops us with a 1008 Pong-timeout), live execution, and signals:request markers.
                opt = await asyncio.to_thread(optimize, template, candles, policy)
                ir, eval_result = opt["ir"], opt["evaluation"]
                if ir is None:
                    log.warning("Strategy attempt %d/%d for ticker %s: optimizer found no scoreable config (%d tried). Discarding.",
                                attempt, MAX_ATTEMPTS, ticker_id, opt.get("tried") or 0)
                    continue
            else:
                ir = parse_generated_strategy(llm_res["data"])
                if ir is None:
                    log.warning("Strategy attempt %d/%d for ticker %s produced an invalid rule tree. Discarding.",
                                attempt, MAX_ATTEMPTS, ticker_id)
                    continue
                if ir.get("direction") in ("short", "both") and not allow_short:
                    log.warning("Strategy attempt %d/%d for ticker %s proposed a SHORT/BOTH strategy on a non-futures market. Discarding.",
                                attempt, MAX_ATTEMPTS, ticker_id)
                    continue
                if _direction_mismatch(ir, attempt, ticker_id):
                    continue
                # Full walk-forward backtest — also CPU-bound; keep it off the event loop (see above).
                eval_result = await asyncio.to_thread(evaluate_strategy, candles, ir, policy)

            last_eval, last_params = eval_result, ir

            if not (eval_result["passedEvaluationGate"] or skip_gate):
                failures = gate_failure_reasons(eval_result, policy)
                log.warning(
                    "Strategy attempt %d/%d for ticker %s failed the gate. Failing conditions: %s. "
                    "(sharpe %s, PF %s, DD %s%%, trades %s, params %s). Discarding.",
                    attempt, MAX_ATTEMPTS, ticker_id, "; ".join(failures) or "unknown",
                    eval_result["sharpe"], eval_result["profitFactor"], eval_result["maxDrawdown"],
                    eval_result["tradeCount"], eval_result["parameterCount"],
                )
                continue

            # --- Passed the gate (or gate bypassed): persist strategy + backtest, then promote. ---
            existing_count = await self.store.count_strategies(ticker_id)
            strategy_id = await self.store.insert_strategy(
                ticker_id=ticker_id, version=existing_count + 1,
                parameters_json=ir, generated_by=llm_res["model"], eval_interval=eval_interval,
            )
            await self.store.insert_backtest(strategy_id=strategy_id, ev=eval_result)
            await self.store.set_onboarding_stage(ticker_id, STAGE_EVALUATING)

            # Retire the previous LIVE strategy (record its lesson first), then promote.
            previous_live = await self.store.get_live_strategy(ticker_id)
            if previous_live and previous_live["id"] != strategy_id:
                try:
                    await self._record_lesson(ticker, previous_live, retirement_reason)
                except Exception as err:  # noqa: BLE001 — lesson recording is non-fatal
                    log.warning("Lesson recording failed (non-fatal): %s", err)
                await self.store.retire_live(ticker_id)

            await self.store.promote_strategy(strategy_id)
            await self.store.set_ticker_active_ready(ticker_id)

            gate_bypassed = skip_gate and not eval_result["passedEvaluationGate"]
            log.info("Strategy v%d promoted LIVE for ticker %s (attempt %d, interval %s%s).",
                     existing_count + 1, ticker_id, attempt, eval_interval,
                     ", GATE BYPASSED" if gate_bypassed else "")
            return {"strategyId": strategy_id, "evaluation": eval_result, "saved": True,
                    "attempts": attempt, "gateBypassed": gate_bypassed}

        # No attempt cleared the gate — nothing is persisted, but record WHY so the next
        # generation cycle can learn from it (spec 4.11 continuous learning, failure path).
        await self.store.set_onboarding_stage(ticker_id, STAGE_FAILED)
        last_failures = gate_failure_reasons(last_eval, policy) if last_eval else []
        if last_eval and last_params:
            try:
                await self._record_failure_lesson(ticker, last_params, last_eval, last_failures, retirement_reason)
            except Exception as err:  # noqa: BLE001 — learning is best-effort, never fail the cycle
                log.warning("Failure-lesson recording failed (non-fatal): %s", err)
        return {
            "strategyId": None, "evaluation": last_eval, "params": last_params,
            "saved": False, "attempts": MAX_ATTEMPTS,
            "failingConditions": last_failures,
            "message": (
                f"No generated strategy passed the evaluation gate after {MAX_ATTEMPTS} attempts."
                + (f" Last attempt failed on: {'; '.join(last_failures)}." if last_failures else "")
            ),
        }

    async def _record_failure_lesson(
        self, ticker: dict, params: dict, evaluation: Optional[dict], failing_conditions: list[str], reason: str
    ) -> None:
        """Record a FAILURE lesson when no attempt cleared the gate (mirrors
        AiLessonsService.recordFailureLesson). Non-fatal — the caller wraps this in try/except."""
        prompt = build_failure_lesson_prompt(ticker["symbol"], params or {}, evaluation, failing_conditions, reason)
        # Deterministic fallback with the concrete params + failing conditions — used whenever the
        # LLM summary is unavailable OR empty. Reasoning models (e.g. deepseek-v4-pro) can spend
        # the whole token budget "thinking" and return no text, which would otherwise persist a
        # blank, useless lesson.
        cfg = (params or {}).get("indicatorConfig") or {}
        conditions = "; ".join(failing_conditions) or "unknown"
        fallback_text = (
            f"A generated strategy for {ticker['symbol']} failed the gate ({conditions}). "
            f"Attempted EMA {cfg.get('emaFastPeriod')}/{cfg.get('emaSlowPeriod')}, "
            f"SL {cfg.get('stopLossPct')}% / TP {cfg.get('takeProfitPct')}%; "
            f"out-of-sample Sharpe {(evaluation or {}).get('sharpe', 'n/a')}, "
            f"profit factor {(evaluation or {}).get('profitFactor', 'n/a')}."
        )
        summary_text = ""
        try:
            llm_res = await self.llm.generate_completion(prompt, max_tokens=800)
            summary_text = (llm_res.get("content") or "").strip()
            await self.llm.log_cost(
                getattr(self.store, "pool", None), ticker["id"], None, LlmPurpose.RE_EVALUATION, llm_res
            )
        except Exception as err:  # noqa: BLE001
            log.warning("Failure-lesson summarization LLM call failed, using a structured fallback: %s", err)
        if not summary_text:
            summary_text = fallback_text

        await self.store.insert_lesson(
            source_strategy_id=None,
            source_divergence_id=None,
            ticker_id=ticker["id"],
            market_type=ticker.get("market_type_name"),
            strategy_type=infer_strategy_type(params),
            regime_tags=[],
            outcome="failure",
            summary_text=summary_text,
        )

    async def _record_lesson(self, ticker: dict, previous_live: dict, reason: str) -> None:
        """Faithful port of AiLessonsService.recordLesson."""
        backtest = await self.store.get_backtest_metrics(previous_live["id"])
        divergence = await self.store.get_latest_divergence(previous_live["id"])

        is_failure = (divergence and divergence.get("flagged")) or (backtest and not backtest.get("passedEvaluationGate"))
        outcome = "failure" if is_failure else "success"

        prompt = build_lesson_prompt(ticker["symbol"], previous_live.get("parametersJson") or {}, backtest, reason, divergence)
        # Deterministic fallback — used whenever the LLM summary is unavailable OR empty (reasoning
        # models can return no text after using the whole budget on hidden reasoning).
        sharpe = backtest.get("sharpe") if backtest else "n/a"
        pf = backtest.get("profitFactor") if backtest else "n/a"
        fallback_text = (
            f"Strategy v{previous_live.get('version')} for {ticker['symbol']} was retired ({reason}). "
            f"Backtest Sharpe {sharpe}, profit factor {pf}."
        )
        summary_text = ""
        try:
            llm_res = await self.llm.generate_completion(prompt, max_tokens=800)
            summary_text = (llm_res.get("content") or "").strip()
            await self.llm.log_cost(
                getattr(self.store, "pool", None), ticker["id"], previous_live["id"], LlmPurpose.RE_EVALUATION, llm_res
            )
        except Exception as err:  # noqa: BLE001
            log.warning("Lesson summarization LLM call failed, using a structured fallback: %s", err)
        if not summary_text:
            summary_text = fallback_text

        await self.store.insert_lesson(
            source_strategy_id=previous_live["id"],
            source_divergence_id=divergence["id"] if divergence else None,
            ticker_id=ticker["id"],
            market_type=ticker.get("market_type_name"),
            strategy_type=infer_strategy_type(previous_live.get("parametersJson")),
            regime_tags=[],
            outcome=outcome,
            summary_text=summary_text,
        )
