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

from ..config import config
from ..llm.schemas import ExitTightenDecision, LiveDecision
from ..llm.service import LlmPurpose, LlmService
from ..strategy.dsl.interpreter import exit_reason, intended_position_state, should_enter
from ..strategy.dsl.ir import resolve_ir, warmup_bars
from ..strategy.generator import format_lessons_for_prompt, infer_strategy_type, _js_num

log = logging.getLogger("worker.execution.signals")

MODE_B = "mode_b_ai_live"


class SignalStore(Protocol):
    async def get_live_strategy(self, ticker_id: str) -> Optional[dict]: ...
    async def load_recent_closes(self, ticker_id: str, limit: int) -> list[float]: ...
    async def load_recent_candles(self, ticker_id: str, limit: int) -> list[dict]: ...
    async def load_recent_candles_for_interval(self, ticker_id: str, interval: str, limit: int) -> list[dict]: ...
    async def get_open_position_for_strategy(self, ticker_id: str, strategy_id: str) -> Optional[dict]: ...
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def retrieve_lessons(self, ticker_id: str, strategy_type: Optional[str], limit: int = 5) -> list[dict]: ...


async def evaluate_rules_signal(store: SignalStore, ticker_id: str, live_strategy: dict) -> str:
    """Mode A — deterministic rule-tree execution via the shared DSL interpreter (a legacy
    indicatorConfig blob is auto-translated). Faithful port of
    strategy-engine.service.ts::evaluateRulesSignal: candles are aggregated to the strategy's OWN
    eval interval (not raw 1m), so a higher-timeframe strategy isn't fired on 1-minute noise, and a
    flat position is reconciled to the strategy's INTENDED exposure (not just a fresh entry edge)."""
    ir = resolve_ir(live_strategy["parametersJson"])
    if not ir:
        return "HOLD"
    if ir.get("direction") == "short":
        # Short strategies are fully backtestable/promotable, but live short EXECUTION needs the
        # futures order path (Phase 4b). Until then the live loop stays flat on them rather than
        # placing a wrong-direction long order.
        log.warning("Live strategy %s is a SHORT strategy; live short execution is not yet wired "
                    "(Phase 4b). Holding.", live_strategy.get("id"))
        return "HOLD"

    warmup = warmup_bars(ir)
    need = max(warmup * 5 + 20, 300)
    # Evaluate on the SAME timeframe this strategy was generated/backtested on (stored per strategy),
    # falling back to the global default — otherwise what trades live wouldn't match what was promoted.
    interval = live_strategy.get("evalInterval") or config.strategy_eval_interval
    candles = await store.load_recent_candles_for_interval(ticker_id, interval, need)
    if len(candles) < warmup + 2:
        return "HOLD"
    i = len(candles) - 1

    open_position = await store.get_open_position_for_strategy(ticker_id, live_strategy["id"])
    if not open_position:
        # Reconcile to the strategy's INTENDED exposure: if its stateful replay says it should
        # currently be holding (entered earlier and hasn't hit an exit), open to match it — not only
        # on a fresh entry edge this bar. Mirrors the backend's shouldEnter || intendedPositionState.
        intended_long = should_enter(ir, candles, i) or intended_position_state(ir, candles) == "LONG"
        return "BUY" if intended_long else "HOLD"

    entry_price = float(open_position["entryPrice"])
    entry_time = open_position.get("openedAt")
    entry_index = i
    if entry_time is not None:
        for k in range(len(candles) - 1, -1, -1):
            if candles[k]["timestamp"] <= entry_time:
                entry_index = k
                break
    bars_held = max(0, i - entry_index)
    # Favorable extreme since entry: highest HIGH for a long, lowest LOW for a short.
    is_short = ir.get("direction") == "short"
    extreme_price = entry_price
    for k in range(entry_index, i + 1):
        if is_short:
            lo = candles[k].get("low", candles[k]["close"])
            if lo < extreme_price:
                extreme_price = lo
        else:
            hi = candles[k].get("high", candles[k]["close"])
            if hi > extreme_price:
                extreme_price = hi

    reason = exit_reason(ir, candles, i, {"entryPrice": entry_price, "barsHeld": bars_held, "extremePrice": extreme_price})
    return "SELL" if reason else "HOLD"


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


async def apply_hybrid_exit_overlay(
    store: SignalStore, llm: LlmService, pool: Any, ticker_id: str, live_strategy: dict, close: float
) -> str:
    """Phase 2 hybrid overlay: on a rules HOLD, let the AI book a WINNING open position early if the
    up-move looks exhausted. Tightens only — returns 'SELL' or 'HOLD', never 'BUY'. Losers are left
    to the deterministic stop/floor, so the AI budget is spent only protecting real gains."""
    open_position = await store.get_open_position_for_strategy(ticker_id, live_strategy["id"])
    if not open_position:
        return "HOLD"  # nothing to protect
    if float(open_position.get("unrealizedPl") or 0) <= 0:
        return "HOLD"  # deterministic stop/floor handles losers; save the LLM call

    ticker = await store.get_ticker(ticker_id)
    if not ticker:
        return "HOLD"
    closes = await store.load_recent_closes(ticker_id, 20)
    price_change_pct = ((closes[-1] - closes[0]) / closes[0]) * 100 if len(closes) >= 2 else 0
    lessons = await store.retrieve_lessons(ticker_id, infer_strategy_type(live_strategy["parametersJson"]))
    lessons_block = format_lessons_for_prompt(lessons)
    entry = float(open_position["entryPrice"])
    unrealized = float(open_position.get("unrealizedPl") or 0)
    ret_pct = ((close - entry) / entry) * 100 if entry else 0

    prompt = (
        f"Open-position exit check for {ticker['symbol']} (Interval: {ticker['interval']}).\n"
        f"You are LONG from {entry}; latest price {_js_num(close)}, unrealized {ret_pct:.2f}% ({unrealized:+.4f}).\n"
        f"Price change over the last {len(closes)} candles: {price_change_pct:.2f}%.\n"
        f"The deterministic rules currently say HOLD (no stop / target / structure-break exit has "
        f"fired). Your ONLY job: judge whether the up-move looks EXHAUSTED or about to reverse, so we "
        f"should EXIT now and bank the profit, or HOLD to keep riding the trend for more.\n\n"
        f"Relevant lessons from past strategies on this ticker/strategy type:\n{lessons_block}\n\n"
        f"Respond in JSON with EXIT or HOLD."
    )
    res = await llm.generate_structured_completion(prompt, ExitTightenDecision,
                                                   schema_name="propose_exit_tighten", max_tokens=512)
    await llm.log_cost(pool, ticker_id, live_strategy["id"], LlmPurpose.LIVE_DECISION,
                       {"model": res["model"], "provider": res.get("provider"),
                        "inputTokens": res["inputTokens"], "outputTokens": res["outputTokens"], "costUsd": res["costUsd"]})
    return "SELL" if res["data"].action == "EXIT" else "HOLD"


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
        # Phase 2 hybrid AI overlay (opt-in): only upgrades a rules HOLD to an early SELL on a
        # winning position; it can never turn a rules SELL into HOLD or open a position.
        if config.hybrid_exit_ai and signal == "HOLD":
            signal = await apply_hybrid_exit_overlay(store, llm, pool, ticker_id, live, close)
    return (signal, live["id"])
