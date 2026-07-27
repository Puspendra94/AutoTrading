"""LLM service — faithful port of llm.service.ts.

Every strategy-generation / re-evaluation / live-decision call goes through this single
service. It walks the LLM_MODELS fallback chain, forces structured output into the given
pydantic schema (provider-native tool-calling / JSON-mode, per-provider method), tracks
cost, and — only when every model in the chain is unreachable — returns the same
deterministic synthetic fallback the backend uses (always suffixed '-fallback' so it's never
mistaken for a real completion).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Type

from pydantic import BaseModel

from .chain_builder import ModelSpec, build_model, extract_text, parse_chain

log = logging.getLogger("worker.llm")


# Mirrors LlmPurpose (llm-cost-log.entity.ts) — the PG enum labels.
class LlmPurpose:
    STRATEGY_GENERATION = "strategy_generation"
    RE_EVALUATION = "re_evaluation"
    LIVE_DECISION = "live_decision"


# Only models with solid current pricing get precise cost tracking; anything else logs at
# $0 rather than a fabricated estimate. Same table as the backend.
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}


def price_for(model_id: str) -> dict[str, float]:
    normalized = re.sub(r"-\d{8}.*$", "", model_id.split(".")[-1]) or model_id
    return PRICING.get(normalized) or PRICING.get(model_id) or {"input": 0.0, "output": 0.0}


# Canned defaults for the deterministic fallback path (identical to llm.service.ts).
_FALLBACK_PARAMS = {
    "strategyName": "Adaptive Trend Breakout",
    "indicatorConfig": {
        "emaFastPeriod": 12,
        "emaSlowPeriod": 26,
        "stopLossPct": 1.5,
        "takeProfitPct": 3.5,
    },
    "reasoning": "Trend-following EMA fast/slow crossover with fixed stop-loss and take-profit on 1m/5m timeframe.",
}


def _cost(input_tokens: int, output_tokens: int, model_id: str) -> float:
    price = price_for(model_id)
    return input_tokens * price["input"] / 1_000_000 + output_tokens * price["output"] / 1_000_000


class LlmService:
    def __init__(self) -> None:
        self.model_chain: list[ModelSpec] = parse_chain()
        log.info(
            "LLM fallback chain: %s",
            " -> ".join(f"{s.provider}:{s.model_id}" for s in self.model_chain),
        )

    # ------------------------------------------------------------------ completions
    async def generate_completion(self, prompt: str, max_tokens: int = 1024) -> dict:
        from langchain_core.messages import HumanMessage

        for spec in self.model_chain:
            try:
                model = build_model(spec, max_tokens)
                result = await model.ainvoke([HumanMessage(content=prompt)])
                text = extract_text(result.content)
                usage = getattr(result, "usage_metadata", None) or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                return {
                    "content": text,
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "costUsd": _cost(input_tokens, output_tokens, spec.model_id),
                    "model": spec.model_id,
                    "provider": spec.provider,
                }
            except Exception as err:  # noqa: BLE001 — try the next model in the chain
                log.warning("Model '%s:%s' failed: %s — trying next in chain.", spec.provider, spec.model_id, err)

        log.warning("All models in LLM_MODELS chain failed — using quantitative fallback.")
        return self._synthetic_fallback(self.model_chain[0].model_id if self.model_chain else None)

    async def generate_structured_completion(
        self,
        prompt: str,
        schema: Type[BaseModel],
        schema_name: str = "propose_strategy_params",  # kept for parity; Python derives the tool name from the class
        max_tokens: int = 2048,  # reasoning models need room to "think" AND still emit complete JSON
    ) -> dict:
        from langchain_core.messages import HumanMessage

        for spec in self.model_chain:
            try:
                model = build_model(spec, max_tokens)
                # DeepSeek reasoning models reject the forced tool_choice the default
                # 'function_calling' method sends, so they need 'json_mode'; bedrock/direct_api
                # keep the default (bedrock throws on json_mode). Matches llm.service.ts.
                kwargs: dict[str, Any] = {"include_raw": True}
                if spec.provider == "deepseek":
                    kwargs["method"] = "json_mode"
                structured_model = model.with_structured_output(schema, **kwargs)
                result = await structured_model.ainvoke([HumanMessage(content=prompt)])

                raw = result.get("raw") if isinstance(result, dict) else None
                usage = (getattr(raw, "usage_metadata", None) or {}) if raw is not None else {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                parsed = result.get("parsed") if isinstance(result, dict) else result
                # LangChain returns parsed=None (instead of raising) when the model's text can't
                # be coerced into the schema — common with reasoning models that truncate their
                # JSON. Treat it as a model failure so the chain falls through to the next model /
                # synthetic fallback, never handing None downstream (which crashes the evaluator).
                if parsed is None:
                    raise ValueError(
                        f"Structured output could not be parsed into the schema (parsed=None) "
                        f"for {spec.provider}:{spec.model_id}"
                    )
                return {
                    "data": parsed,
                    "inputTokens": input_tokens,
                    "outputTokens": output_tokens,
                    "costUsd": _cost(input_tokens, output_tokens, spec.model_id),
                    "model": spec.model_id,
                    "provider": spec.provider,
                }
            except Exception as err:  # noqa: BLE001 — try the next model in the chain
                log.warning(
                    "Model '%s:%s' failed structured call: %s — trying next in chain.",
                    spec.provider, spec.model_id, err,
                )

        log.warning("All models in LLM_MODELS chain failed structured output — using quantitative fallback.")
        return self._synthetic_structured_fallback(schema, self.model_chain[0].model_id if self.model_chain else None)

    # ------------------------------------------------------------------ fallbacks
    def _synthetic_fallback(self, requested_model: str | None) -> dict:
        model_name = requested_model or "unknown-model"
        input_tokens, output_tokens = 210, 140
        return {
            "content": json.dumps(_FALLBACK_PARAMS),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "costUsd": (input_tokens * 3 + output_tokens * 15) / 1_000_000,
            "model": f"{model_name}-fallback",
        }

    def _synthetic_structured_fallback(self, schema: Type[BaseModel], requested_model: str | None) -> dict:
        model_name = requested_model or "unknown-model"
        input_tokens, output_tokens = 210, 140
        return {
            "data": schema.model_validate(_FALLBACK_PARAMS),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "costUsd": (input_tokens * 3 + output_tokens * 15) / 1_000_000,
            "model": f"{model_name}-fallback",
        }

    # ------------------------------------------------------------------ cost logging
    async def log_cost(
        self,
        pool,
        ticker_id: str | None,
        strategy_id: str | None,
        purpose: str,
        response: dict,
    ) -> None:
        """Persist one llm_cost_log row (mirrors LlmService.logCost). No-ops without a pool
        (e.g. in unit tests with a fake store)."""
        if pool is None:
            return
        provider = "fallback" if str(response["model"]).endswith("-fallback") else (response.get("provider") or "unknown")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_cost_log
                    (ticker_id, strategy_id, purpose, llm_provider, model, input_tokens, output_tokens, cost_usd)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                ticker_id,
                strategy_id,
                purpose,
                provider,
                response["model"],
                int(response["inputTokens"]),
                int(response["outputTokens"]),
                response["costUsd"],
            )
