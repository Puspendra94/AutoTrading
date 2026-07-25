"""Live-signal evaluation — faithful port of strategy-engine.service.ts::evaluateLiveSignal
(Phase 3c-3): dispatches to Mode A (deterministic EMA-crossover rules, zero LLM calls) or
Mode B (per-decision AI live decision) based on the live strategy's executionMode.

Mode A mirrors exactly what strategy-evaluator backtests, so what executes live matches what
was promoted. Mode B sends the LLM only summarized structured stats (never raw candles) plus
relevant lessons.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Protocol

from ..llm.schemas import LiveDecision
from ..llm.service import LlmPurpose, LlmService
from ..strategy.evaluator import _calculate_ema
from ..strategy.generator import format_lessons_for_prompt, infer_strategy_type, _js_num

log = logging.getLogger("worker.execution.signals")

MODE_B = "mode_b_ai_live"


class SignalStore(Protocol):
    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]: ...
    async def load_recent_closes(self, ticker_id: str, limit: int) -> list[float]: ...
    async def get_open_position_for_strategy(self, ticker_id: str, strategy_id: str) -> Optional[dict]: ...
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def retrieve_lessons(self, ticker_id: str, strategy_type: Optional[str], limit: int = 5) -> list[dict]: ...


async def evaluate_rules_signal(store: SignalStore, ticker_id: str, live_strategy: dict) -> str:
    """Mode A — deterministic EMA fast/slow crossover with SL/TP exits."""
    cfg = (live_strategy["parametersJson"] or {}).get("indicatorConfig") or {}
    ema_fast_period = cfg.get("emaFastPeriod") or 12
    ema_slow_period = cfg.get("emaSlowPeriod") or 26
    stop_loss_pct = (cfg.get("stopLossPct") or 1.5) / 100
    take_profit_pct = (cfg.get("takeProfitPct") or 3.5) / 100

    closes = await store.load_recent_closes(ticker_id, ema_slow_period + 5)
    if len(closes) < ema_slow_period + 2:
        return "HOLD"

    fast_ema = _calculate_ema(closes, ema_fast_period)
    slow_ema = _calculate_ema(closes, ema_slow_period)
    i = len(closes) - 1
    price = closes[i]

    open_position = await store.get_open_position_for_strategy(ticker_id, live_strategy["id"])
    if not open_position:
        bullish_cross = fast_ema[i] > slow_ema[i] and fast_ema[i - 1] <= slow_ema[i - 1]
        return "BUY" if bullish_cross else "HOLD"

    entry_price = float(open_position["entryPrice"])
    return_pct = (price - entry_price) / entry_price
    is_stop_loss = return_pct <= -stop_loss_pct
    is_take_profit = return_pct >= take_profit_pct
    is_cross_down = fast_ema[i] < slow_ema[i]
    return "SELL" if (is_stop_loss or is_take_profit or is_cross_down) else "HOLD"


async def evaluate_mode_b_live_decision(
    store: SignalStore, llm: LlmService, pool: Any, ticker_id: str, live_strategy: dict, close: float
) -> str:
    """Mode B — AI live decision. Only summarized stats + lessons reach the LLM."""
    ticker = await store.get_ticker(ticker_id)
    if not ticker:
        return "HOLD"

    open_position = await store.get_open_position_for_strategy(ticker_id, live_strategy["id"])
    closes = await store.load_recent_closes(ticker_id, 20)
    price_change_pct = ((closes[-1] - closes[0]) / closes[0]) * 100 if len(closes) >= 2 else 0

    lessons = await store.retrieve_lessons(ticker_id, infer_strategy_type(live_strategy["parametersJson"]))
    lessons_block = format_lessons_for_prompt(lessons)
    position_block = (
        f"Currently holding a position: entry price {float(open_position['entryPrice'])}, "
        f"unrealized P/L {float(open_position['unrealizedPl'])}."
        if open_position else "Currently flat (no open position)."
    )

    prompt = (
        f"Live trading decision for {ticker['symbol']} (Interval: {ticker['interval']}).\n"
        f"Latest close: {_js_num(close)}. Price change over last {len(closes)} candles: {price_change_pct:.2f}%.\n"
        f"{position_block}\n"
        f"Strategy context: {json.dumps(live_strategy['parametersJson'], separators=(',', ':'))}.\n"
        f"\n"
        f"Relevant lessons from past strategies on this ticker/strategy type:\n{lessons_block}\n"
        f"\n"
        f"Decide the trading action right now: BUY (enter/add long), SELL (exit an open position), "
        f"or HOLD (do nothing).\n"
        f"Respond in JSON with your decision."
    )

    res = await llm.generate_structured_completion(prompt, LiveDecision, schema_name="propose_live_decision", max_tokens=512)
    await llm.log_cost(pool, ticker_id, live_strategy["id"], LlmPurpose.LIVE_DECISION,
                       {"model": res["model"], "provider": res.get("provider"),
                        "inputTokens": res["inputTokens"], "outputTokens": res["outputTokens"], "costUsd": res["costUsd"]})

    action = res["data"].action
    # Never open a second position or exit one that doesn't exist (gate enforces 5.2 anyway).
    if action == "BUY" and open_position:
        return "HOLD"
    if action == "SELL" and not open_position:
        return "HOLD"
    return action


async def evaluate_live_signal(
    store: SignalStore, llm: LlmService, pool: Any, ticker_id: str, close: float
) -> tuple[str, Optional[str]]:
    """Returns (signal, strategyId). HOLD/None when no live strategy exists."""
    live = await store.get_live_strategy(ticker_id)
    if not live:
        return ("HOLD", None)
    if live.get("executionMode") == MODE_B:
        signal = await evaluate_mode_b_live_decision(store, llm, pool, ticker_id, live, close)
    else:
        signal = await evaluate_rules_signal(store, ticker_id, live)
    return (signal, live["id"])
