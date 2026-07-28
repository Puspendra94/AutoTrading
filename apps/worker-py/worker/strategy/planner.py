"""Meta-planner — port of strategy-engine.service.ts::planGeneration.

Before generating a strategy, the AI reads accumulated lessons (failed generations + live-perf
reviews) and decides HOW to build the next one: timeframe, how much data, which signals to lean on,
and (within HARD SAFE FLOORS it can never breach) how strict the gate should be. The proposed gate
thresholds are only a REQUEST — they're clamped to floors here so the generator can never weaken its
own test into meaninglessness.
"""
from __future__ import annotations

import json
import logging

from ..config import config
from ..llm.schemas import GenerationPlan
from ..llm.service import LlmPurpose
from .generator import format_lessons_for_prompt

log = logging.getLogger("worker.strategy.planner")


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


async def plan_generation(store, llm, pool, ticker: dict, active_policy: dict) -> dict:
    """Return {interval, candleLimit, policy(effective, clamped), signalEmphasis, reasoning}."""
    default_plan = {
        "interval": config.strategy_eval_interval,
        "candleLimit": config.strategy_eval_candle_limit,
        "policy": active_policy,
        "signalEmphasis": "",
        "reasoning": "default plan (planner unavailable / no lessons yet)",
    }

    lessons = await store.retrieve_lessons(ticker["id"], None, 12)
    if not lessons:
        return default_plan  # nothing to adapt from yet
    lessons_block = format_lessons_for_prompt(lessons)

    plan_prompt = (
        f"You are planning HOW to generate the next automated trading strategy for {ticker['symbol']}, "
        f"BEFORE it is built. The generator composes a rule tree from EXACTLY these indicators (do not ask for "
        f"any indicator not in this list — e.g. no supertrend, no VWAP): EMA, SMA, RSI, MACD, ATR, ADX "
        f"(trend strength 0-100, >25 = trending — usable as a trend filter), Bollinger, Donchian, Stochastic, "
        f"rollingHigh, rollingLow, plus raw price/volume. Approaches: "
        f"trend-following, mean-reversion, or channel breakouts.\n"
        f"IMPORTANT lesson from this asset: plain moving-average CROSSOVERS repeatedly fail here (they whipsaw "
        f"and bleed fees). Unless the lessons clearly show a crossover working, steer the generator AWAY from "
        f"MA crossovers and TOWARD mean-reversion oscillators (e.g. RSI or Stochastic OVERSOLD entry with a "
        f"200-EMA uptrend filter, exit when the oscillator recovers) or channel breakouts (Donchian/Bollinger). "
        f"Mean-reversion oscillator strategies use two levels (an oversold entry and a recovery exit) plus a "
        f"trend filter — that fits within the parameter budget, so prefer that shape over another crossover.\n"
        f"CALIBRATION for mean-reversion: use a MODERATE oversold level — RSI below ~35 (NOT below 30: below 30 "
        f"fires too rarely to clear the trade count) or Stochastic below ~25 — and exit on recovery (RSI above "
        f"~55). Oversold dips in an uptrend are INFREQUENT, so keep minTradeCount modest (5-8); do not demand "
        f"more trades than an oversold-dip setup can realistically produce, or you will reject good strategies "
        f"purely on trade count. Make signalEmphasis internally consistent with the minTradeCount you pick.\n"
        f"Choose the setup most likely to CLEAR THE GATE and hold up. Base every choice on the lessons: too few "
        f"trades -> shorter interval or more candles; an approach repeatedly failing -> switch families. If "
        f"lessons show setups landing just under Sharpe 1 with a healthy profit factor (>1.3), you MAY lower "
        f"minSharpe toward its 0.5 floor — the thresholds you pick are clamped to hard floors (minSharpe>=0.5, "
        f"minProfitFactor>=1.2, maxDrawdownPct<=25, minTradeCount>=5). Current defaults: interval "
        f"{config.strategy_eval_interval}, candleLimit {config.strategy_eval_candle_limit}, gate minSharpe "
        f"{active_policy['minSharpe']} / minProfitFactor {active_policy['minProfitFactor']} / maxDrawdownPct "
        f"{active_policy['maxDrawdownPct']} / minTradeCount {active_policy['minTradeCount']}.\n"
        f"\n"
        f'Respond in JSON with exactly these fields: interval (one of "1h","2h","4h","6h","12h","1d"), '
        f"candleLimit (integer 1000-20000), minSharpe (number 0.5-3), minProfitFactor (number 1.2-3), "
        f"maxDrawdownPct (number 5-25), minTradeCount (integer 5-300), signalEmphasis (string: concrete "
        f"guidance for the generator — which indicator family/approach and structure to favor or avoid given "
        f"the lessons; name a SPECIFIC non-crossover shape when crossovers have failed), reasoning (string).\n"
        f"\n"
        f"Lessons:\n{lessons_block}"
    )

    try:
        res = await llm.generate_structured_completion(plan_prompt, GenerationPlan, schema_name="plan_generation")
        plan: GenerationPlan = res["data"]
        await llm.log_cost(pool, ticker["id"], None, LlmPurpose.STRATEGY_GENERATION,
                           {"model": res["model"], "provider": res.get("provider"),
                            "inputTokens": res["inputTokens"], "outputTokens": res["outputTokens"],
                            "costUsd": res["costUsd"]})
    except Exception as err:  # noqa: BLE001 — planning is best-effort; fall back to static defaults
        log.warning("Generation planner failed, using defaults: %s", err)
        return default_plan

    effective_policy = {
        "minSharpe": _clamp(float(plan.minSharpe), 0.5, 3),
        "minProfitFactor": _clamp(float(plan.minProfitFactor), 1.2, 3),
        "maxDrawdownPct": _clamp(float(plan.maxDrawdownPct), 5, 25),
        "minTradeCount": round(_clamp(float(plan.minTradeCount), 5, 300)),
        "maxParameterCount": active_policy["maxParameterCount"],  # fixed — never AI-controlled
    }
    log.info(
        "Generation plan for %s: interval %s, candleLimit %s, gate proposed %s/%s/%s/%s -> clamped %s/%s/%s/%s. "
        "Emphasis: %s",
        ticker["symbol"], plan.interval, plan.candleLimit, plan.minSharpe, plan.minProfitFactor,
        plan.maxDrawdownPct, plan.minTradeCount, effective_policy["minSharpe"], effective_policy["minProfitFactor"],
        effective_policy["maxDrawdownPct"], effective_policy["minTradeCount"], plan.signalEmphasis,
    )
    return {"interval": plan.interval, "candleLimit": int(plan.candleLimit), "policy": effective_policy,
            "signalEmphasis": plan.signalEmphasis, "reasoning": plan.reasoning}
