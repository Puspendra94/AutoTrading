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
from .dsl.indicators import adx, ema
from .dsl.interpreter import build_series
from .generator import format_lessons_for_prompt

log = logging.getLogger("worker.strategy.planner")

FUTURES = "futures"


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _pct_change(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-bars - 1] == 0:
        return None
    return ((closes[-1] - closes[-bars - 1]) / closes[-bars - 1]) * 100


def _summarize_regime(candles: list[dict]) -> dict:
    """Describe the CURRENT market regime so the planner can pick a direction that suits it
    (rather than always prescribing an uptrend-filtered long). Returns {bias, text}; bias is
    'bullish' | 'bearish' | 'mixed' | 'unknown'."""
    if len(candles) < 210:
        return {"bias": "unknown", "text": "Not enough history to classify the current regime."}

    s = build_series(candles)
    closes = s["close"]
    i = len(closes) - 1
    price = closes[i]
    e100, e200 = ema(closes, 100)[i], ema(closes, 200)[i]
    trend_strength = adx(s, 14)[i]

    above_100 = price > e100
    above_200 = price > e200
    if above_100 and above_200:
        bias = "bullish"
    elif not above_100 and not above_200:
        bias = "bearish"
    else:
        bias = "mixed"

    parts = [
        f"price {price:.2f} is {'ABOVE' if above_100 else 'BELOW'} its 100-EMA ({e100:.2f}) and "
        f"{'ABOVE' if above_200 else 'BELOW'} its 200-EMA ({e200:.2f})",
    ]
    if trend_strength == trend_strength:  # not NaN
        parts.append(f"ADX(14) is {trend_strength:.1f} ({'trending' if trend_strength > 25 else 'ranging'})")
    for bars in (30, 90):
        chg = _pct_change(closes, bars)
        if chg is not None:
            parts.append(f"{chg:+.1f}% over the last {bars} bars")
    return {"bias": bias, "text": f"Current regime is {bias.upper()}: " + "; ".join(parts) + "."}


async def plan_generation(store, llm, pool, ticker: dict, active_policy: dict) -> dict:
    """Return {interval, candleLimit, policy(effective, clamped), signalEmphasis, reasoning}."""
    default_plan = {
        "interval": config.strategy_eval_interval,
        "candleLimit": config.strategy_eval_candle_limit,
        "policy": active_policy,
        "signalEmphasis": "",
        "preferredDirection": None,
        "reasoning": "default plan (planner unavailable / no lessons yet)",
    }

    lessons = await store.retrieve_lessons(ticker["id"], None, 12)
    if not lessons:
        return default_plan  # nothing to adapt from yet
    lessons_block = format_lessons_for_prompt(lessons)

    # On a futures ticker the generator may compose SHORTS, so the planner must be able to steer
    # toward one — otherwise it keeps prescribing uptrend-filtered longs and the system sits flat
    # through every downtrend. Regime is read on the default interval (the plan picks the final one).
    allow_short = (ticker.get("market_type_name") or "").lower() == FUTURES
    regime = {"bias": "unknown", "text": ""}
    try:
        regime_candles = await store.load_candles_for_interval(
            ticker["id"], config.strategy_eval_interval, 1000)
        regime = _summarize_regime(regime_candles)
    except Exception as err:  # noqa: BLE001 — regime is advisory; never block planning on it
        log.warning("Could not read market regime for planning: %s", err)

    if allow_short:
        direction_block = (
            f"MARKET TYPE: FUTURES — the generator can go LONG, SHORT, or BOTH. {regime['text']}\n"
            f"Pick what FITS THIS REGIME rather than defaulting to long. A one-sided strategy only "
            f"trades while its regime lasts: a long that requires an uptrend filter sits flat through "
            f"every downtrend, and a short does the reverse once the market turns.\n"
            f"PREFER **BOTH** unless the lessons give a strong reason not to. A BOTH strategy pairs an "
            f"oversold-entry long (price above the trend EMA) with the mirrored overbought-entry short "
            f"(price below it), so it keeps trading through a regime change instead of going idle. "
            f"Because the two sides SHARE their indicators and periods and only mirror the thresholds, "
            f"it costs about ONE extra tunable knob, and it roughly DOUBLES the number of trades — "
            f"which directly helps clear minTradeCount, the condition that has rejected otherwise "
            f"strong candidates here.\n"
            f"Choose a single side only when one regime clearly dominates the whole backtest window: "
            f"SHORT in a sustained downtrend (overbought entry, RSI above ~65 / Stochastic above ~75, "
            f"price BELOW the trend EMA), LONG in a sustained uptrend (the mirror image).\n"
            f"State the intended direction explicitly in signalEmphasis, using the word LONG, SHORT or "
            f"BOTH — and when you choose BOTH, describe both legs.\n"
        )
    else:
        direction_block = (
            f"MARKET TYPE: SPOT — LONG ONLY (the generator can only buy then sell; it cannot short). "
            f"{regime['text']}\n"
        )

    plan_prompt = (
        f"You are planning HOW to generate the next automated trading strategy for {ticker['symbol']}, "
        f"BEFORE it is built. The generator composes a rule tree from EXACTLY these indicators (do not ask for "
        f"any indicator not in this list — e.g. no supertrend, no VWAP): EMA, SMA, RSI, MACD, ATR, ADX "
        f"(trend strength 0-100, >25 = trending — usable as a trend filter), Bollinger, Donchian, Stochastic, "
        f"rollingHigh, rollingLow, plus raw price/volume. Approaches: "
        f"trend-following, mean-reversion, or channel breakouts.\n"
        f"{direction_block}"
        f"IMPORTANT lesson from this asset: plain moving-average CROSSOVERS repeatedly fail here (they whipsaw "
        f"and bleed fees). Unless the lessons clearly show a crossover working, steer the generator AWAY from "
        f"MA crossovers and TOWARD mean-reversion oscillators (an RSI/Stochastic entry at the extreme that suits "
        f"the chosen direction — OVERSOLD for a long, OVERBOUGHT for a short — plus a 200-EMA trend filter in "
        f"that same direction, exiting when the oscillator reverts) or channel breakouts (Donchian/Bollinger). "
        f"Mean-reversion oscillator strategies use two levels (an entry extreme and a reversion exit) plus a "
        f"trend filter — that fits within the parameter budget, so prefer that shape over another crossover.\n"
        f"CALIBRATION for mean-reversion: use a MODERATE extreme — for a LONG, RSI below ~35 (NOT below 30: below "
        f"30 fires too rarely to clear the trade count) or Stochastic below ~25, exiting on recovery (RSI above "
        f"~55); for a SHORT, mirror it (RSI above ~65 / Stochastic above ~75, exiting as RSI falls back toward "
        f"~45). Such counter-trend extremes are INFREQUENT, so keep minTradeCount modest (5-8); do not demand "
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
        f"maxDrawdownPct (number 5-25), minTradeCount (integer 5-300), preferredDirection (\"long\", "
        f"\"short\" or \"both\" — must match signalEmphasis; on futures prefer \"both\"), "
        f"signalEmphasis (string: concrete "
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
        "Generation plan for %s [%s, regime %s]: interval %s, candleLimit %s, gate proposed %s/%s/%s/%s -> "
        "clamped %s/%s/%s/%s. Emphasis: %s",
        ticker["symbol"], "futures (long/short)" if allow_short else "spot (long only)", regime["bias"],
        plan.interval, plan.candleLimit, plan.minSharpe, plan.minProfitFactor,
        plan.maxDrawdownPct, plan.minTradeCount, effective_policy["minSharpe"], effective_policy["minProfitFactor"],
        effective_policy["maxDrawdownPct"], effective_policy["minTradeCount"], plan.signalEmphasis,
    )
    return {"interval": plan.interval, "candleLimit": int(plan.candleLimit), "policy": effective_policy,
            "signalEmphasis": plan.signalEmphasis,
            # Only meaningful where shorts are allowed; a spot ticker is always long.
            "preferredDirection": plan.preferredDirection if allow_short else "long",
            "reasoning": plan.reasoning}
