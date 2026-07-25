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

import json
import logging
from typing import Any, Optional, Protocol

from ..llm.schemas import StrategyParams
from ..llm.service import LlmPurpose, LlmService
from .evaluator import evaluate_strategy

log = logging.getLogger("worker.strategy.generator")

MAX_ATTEMPTS = 3

# Ticker onboarding stages (mirror OnboardingStage in ticker.entity.ts).
STAGE_GENERATING = "generating_strategy"
STAGE_BACKTESTING = "backtesting"
STAGE_EVALUATING = "evaluating"
STAGE_READY = "ready"
STAGE_FAILED = "failed"


# ---------------------------------------------------------------- pure helpers
def infer_strategy_type(params: Any) -> str:
    """Coarse tag from the indicator config (mirrors inferStrategyTypeTag / inferStrategyType)."""
    cfg = (params or {}).get("indicatorConfig") if isinstance(params, dict) else None
    if cfg and cfg.get("rsiPeriod"):
        return "ema_rsi_trend"
    if cfg and cfg.get("emaFastPeriod"):
        return "ema_crossover"
    return "unclassified"


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


def build_generation_prompt(symbol: str, interval: str, latest_price: float, lessons_block: str) -> str:
    """Byte-for-byte the summaryPrompt built in generateStrategyForTicker."""
    return (
        f"Analyze ticker {symbol} (Interval: {interval}, Latest Price: {_js_num(latest_price)}).\n"
        "Propose optimal quantitative indicator parameters for an automated trend-following trading strategy: EMA\n"
        "fast/slow crossover periods, optional RSI filter, and stop-loss/take-profit percentages.\n"
        "\n"
        "Respond in JSON with exactly these fields: strategyName (string), indicatorConfig.emaFastPeriod (integer, candles),\n"
        "indicatorConfig.emaSlowPeriod (integer, candles), indicatorConfig.rsiPeriod (integer, optional),\n"
        "indicatorConfig.rsiBuyThreshold (0-100, optional), indicatorConfig.rsiSellThreshold (0-100, optional),\n"
        "indicatorConfig.stopLossPct (percent, e.g. 1.5), indicatorConfig.takeProfitPct (percent, e.g. 3.5), and\n"
        "reasoning (string).\n"
        "\n"
        "Relevant lessons from past strategies on this ticker/strategy type (steer away from documented failures, keep\n"
        f"successful approaches in mind):\n{lessons_block}"
    )


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
        f"pattern/cause, not a trade-by-trade recap. Ticker: {symbol}. Strategy parameters: {json.dumps(params, separators=(',', ':'))}. "
        f"Backtest metrics: {json.dumps(metrics, separators=(',', ':'))}. Retirement reason: {reason}. "
        f"Live-vs-backtest divergence: {divergence_str}. Respond with plain text only, no JSON."
    )


# ---------------------------------------------------------------- store protocol
class GeneratorStore(Protocol):
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def has_unresolved_blocking_flags(self, ticker_id: str) -> bool: ...
    async def set_onboarding_stage(self, ticker_id: str, stage: str) -> None: ...
    async def load_recent_candles(self, ticker_id: str, limit: int = 2000) -> list[dict]: ...
    async def get_latest_strategy_params(self, ticker_id: str) -> Optional[dict]: ...
    async def get_active_policy(self) -> dict: ...
    async def count_strategies(self, ticker_id: str) -> int: ...
    async def insert_strategy(self, *, ticker_id: str, version: int, parameters_json: dict, generated_by: str) -> str: ...
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

    async def generate(self, ticker_id: str, retirement_reason: str = "new strategy generation cycle") -> dict:
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

        candles = await self.store.load_recent_candles(ticker_id, 2000)

        prior = await self.store.get_latest_strategy_params(ticker_id)
        strategy_type = infer_strategy_type(prior) if prior else None
        lessons = await self.store.retrieve_lessons(ticker_id, strategy_type)
        lessons_block = format_lessons_for_prompt(lessons)

        latest_price = float(candles[-1]["close"]) if candles else 0
        prompt = build_generation_prompt(ticker["symbol"], ticker["interval"], latest_price, lessons_block)

        policy = await self.store.get_active_policy()

        await self.store.set_onboarding_stage(ticker_id, STAGE_BACKTESTING)

        last_eval = None
        last_params = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            llm_res = await self.llm.generate_structured_completion(prompt, StrategyParams, schema_name="propose_strategy_params")
            parsed: StrategyParams = llm_res["data"]
            # exclude_none so unset optional indicator fields are absent (matches zod's
            # omission in the backend), keeping stored params + parameter count identical.
            params_dict = parsed.model_dump(exclude_none=True)
            eval_result = evaluate_strategy(candles, params_dict, policy)
            last_eval, last_params = eval_result, params_dict

            await self.llm.log_cost(
                getattr(self.store, "pool", None),
                ticker_id, None, LlmPurpose.STRATEGY_GENERATION,
                {"model": llm_res["model"], "provider": llm_res.get("provider"),
                 "inputTokens": llm_res["inputTokens"], "outputTokens": llm_res["outputTokens"],
                 "costUsd": llm_res["costUsd"]},
            )

            if not eval_result["passedEvaluationGate"]:
                log.warning(
                    "Strategy attempt %d/%d for ticker %s failed the gate (sharpe %s, PF %s, DD %s%%, trades %s). Discarding.",
                    attempt, MAX_ATTEMPTS, ticker_id, eval_result["sharpe"], eval_result["profitFactor"],
                    eval_result["maxDrawdown"], eval_result["tradeCount"],
                )
                continue

            # --- Passed the gate: persist strategy + backtest, then promote it live. ---
            existing_count = await self.store.count_strategies(ticker_id)
            strategy_id = await self.store.insert_strategy(
                ticker_id=ticker_id, version=existing_count + 1,
                parameters_json=params_dict, generated_by=llm_res["model"],
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

            log.info("Strategy v%d promoted LIVE for ticker %s (attempt %d).", existing_count + 1, ticker_id, attempt)
            return {"strategyId": strategy_id, "evaluation": eval_result, "saved": True, "attempts": attempt}

        # No attempt cleared the gate — nothing is persisted.
        await self.store.set_onboarding_stage(ticker_id, STAGE_FAILED)
        return {
            "strategyId": None, "evaluation": last_eval, "params": last_params,
            "saved": False, "attempts": MAX_ATTEMPTS,
            "message": f"No generated strategy passed the evaluation gate after {MAX_ATTEMPTS} attempts.",
        }

    async def _record_lesson(self, ticker: dict, previous_live: dict, reason: str) -> None:
        """Faithful port of AiLessonsService.recordLesson."""
        backtest = await self.store.get_backtest_metrics(previous_live["id"])
        divergence = await self.store.get_latest_divergence(previous_live["id"])

        is_failure = (divergence and divergence.get("flagged")) or (backtest and not backtest.get("passedEvaluationGate"))
        outcome = "failure" if is_failure else "success"

        prompt = build_lesson_prompt(ticker["symbol"], previous_live.get("parametersJson") or {}, backtest, reason, divergence)
        try:
            llm_res = await self.llm.generate_completion(prompt, max_tokens=200)
            summary_text = llm_res["content"].strip()
            await self.llm.log_cost(
                getattr(self.store, "pool", None), ticker["id"], previous_live["id"], LlmPurpose.RE_EVALUATION, llm_res
            )
        except Exception as err:  # noqa: BLE001
            log.warning("Lesson summarization LLM call failed, using a structured fallback: %s", err)
            sharpe = backtest.get("sharpe") if backtest else "n/a"
            pf = backtest.get("profitFactor") if backtest else "n/a"
            summary_text = (
                f"Strategy v{previous_live.get('version')} for {ticker['symbol']} was retired ({reason}). "
                f"Backtest Sharpe {sharpe}, profit factor {pf}."
            )

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
