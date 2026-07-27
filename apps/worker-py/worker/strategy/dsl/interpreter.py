"""The one interpreter that executes any StrategyIR rule tree — port of
apps/backend/src/modules/strategy/dsl/interpreter.ts.

Same operand resolution, same NaN-safe condition eval, and the SAME fill/slippage/fee/equity
accounting the pre-DSL engine used (0.05% slippage per side, 0.15% round-trip fee) so metrics
stay comparable. Diffed against the TS by tools/parity_dsl.py.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from . import indicators as ind
from .ir import warmup_bars


def _n(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def build_series(candles: list[dict]) -> dict:
    def pick(c, key):
        v = c.get(key)
        return _n(v if v is not None else c["close"])
    return {
        "open": [pick(c, "open") for c in candles],
        "high": [pick(c, "high") for c in candles],
        "low": [pick(c, "low") for c in candles],
        "close": [_n(c["close"]) for c in candles],
        "volume": [_n(c.get("volume") if c.get("volume") is not None else 0) for c in candles],
    }


def _series_for_operand(o: dict, s: dict, cache: dict) -> list[float]:
    op = o.get("op")
    if op == "const":
        return [float(o["value"])] * len(s["close"])
    if op == "price":
        return s[o["field"]]
    key = json.dumps(o, sort_keys=True)
    if key in cache:
        return cache[key]
    src = s[o.get("source") or "close"]
    kind = o["kind"]
    if kind == "ema":
        arr = ind.ema(src, o["period"])
    elif kind == "sma":
        arr = ind.sma(src, o["period"])
    elif kind == "rsi":
        arr = ind.rsi(src, o["period"])
    elif kind == "atr":
        arr = ind.atr(s, o["period"])
    elif kind == "macd":
        m = ind.macd(src, o["fast"], o["slow"], o["signal"])
        arr = m["signal"] if o["field"] == "signal" else m["hist"] if o["field"] == "hist" else m["line"]
    elif kind == "bollinger":
        b = ind.bollinger(src, o["period"], o["mult"])
        arr = b["upper"] if o["field"] == "upper" else b["lower"] if o["field"] == "lower" else b["mid"]
    elif kind == "donchian":
        d = ind.donchian(s, o["period"])
        arr = d["upper"] if o["field"] == "upper" else d["lower"]
    elif kind == "stochastic":
        st = ind.stochastic(s, o["period"], o["smoothK"], o["smoothD"])
        arr = st["d"] if o["field"] == "d" else st["k"]
    elif kind == "rollingHigh":
        arr = ind.rolling_max(s["high"], o["period"])
    elif kind == "rollingLow":
        arr = ind.rolling_min(s["low"], o["period"])
    else:
        arr = [float("nan")] * len(s["close"])
    cache[key] = arr
    return arr


def _fin(x: float) -> bool:
    return math.isfinite(x)


def eval_condition(c: dict, s: dict, cache: dict, i: int) -> bool:
    op = c["op"]
    if op == "and":
        return all(eval_condition(sub, s, cache, i) for sub in c["conditions"])
    if op == "or":
        return any(eval_condition(sub, s, cache, i) for sub in c["conditions"])
    if op == "not":
        return not eval_condition(c["condition"], s, cache, i)
    left = _series_for_operand(c["left"], s, cache)
    right = _series_for_operand(c["right"], s, cache)
    a, b = left[i], right[i]
    if op == "gt":
        return _fin(a) and _fin(b) and a > b
    if op == "lt":
        return _fin(a) and _fin(b) and a < b
    if op == "gte":
        return _fin(a) and _fin(b) and a >= b
    if op == "lte":
        return _fin(a) and _fin(b) and a <= b
    if op == "crossAbove":
        ap, bp = left[i - 1], right[i - 1]
        return _fin(a) and _fin(b) and _fin(ap) and _fin(bp) and a > b and ap <= bp
    if op == "crossBelow":
        ap, bp = left[i - 1], right[i - 1]
        return _fin(a) and _fin(b) and _fin(ap) and _fin(bp) and a < b and ap >= bp
    return False


def _exit_decision(ir: dict, s: dict, cache: dict, i: int, ctx: dict) -> Optional[str]:
    """Adaptive exit ladder (Phase 1). Deterministic and fully backtestable; a live AI overlay
    (Phase 2) may only TIGHTEN this, never loosen it. Mirrors exitDecision in interpreter.ts."""
    price = s["close"][i]
    entry_price = ctx["entryPrice"]
    bars_held = ctx["barsHeld"]
    peak_price = ctx["peakPrice"]
    return_pct = (price - entry_price) / entry_price
    peak_return = (peak_price - entry_price) / entry_price if peak_price > 0 else return_pct
    risk = ir["risk"]

    # 1. Hard stop-loss — absolute safety backstop, never gated.
    if return_pct <= -risk["stopLossPct"] / 100:
        return "Stop loss"

    # 2. Profit-protection floor — active whenever a trigger is configured (resolve_ir injects the
    #    system default so every real strategy has it). Once peak gain reaches the trigger, the trade
    #    may never round-trip below the floor. This is what stops an 8% gain becoming a 2% loss.
    #    Absent trigger => off (preserves pre-DSL parity for raw IRs that never went through resolve).
    be_trigger = risk.get("breakevenTriggerPct")
    if be_trigger is not None:
        be_floor = risk.get("breakevenFloorPct")
        be_floor = 0.0 if be_floor is None else be_floor
        if peak_return >= be_trigger / 100 and return_pct <= be_floor / 100:
            return "Profit floor"

    # 3. Take-profit target. hard = book immediately; soft = ride past it under a tight trail (#4).
    #    Absent mode => 'hard' (legacy behavior for raw IRs).
    tp_mode = risk.get("takeProfitMode") or "hard"
    if return_pct >= risk["takeProfitPct"] / 100 and tp_mode == "hard":
        return "Take profit"

    # 4. Trailing stop off the peak. The strategy's own trail (if any) is always active; in soft-TP
    #    mode a tighter post-target trail kicks in once the peak has reached the target.
    eff_trail = risk.get("trailingStopPct")
    if tp_mode == "soft" and peak_return >= risk["takeProfitPct"] / 100:
        post_trail = risk.get("postTargetTrailPct")
        post_trail = 2.0 if post_trail is None else post_trail
        eff_trail = post_trail if eff_trail is None else min(eff_trail, post_trail)
    if eff_trail is not None and peak_price > 0:
        drop = (peak_price - price) / peak_price
        if drop >= eff_trail / 100:
            return "Trailing stop"

    # 5. Time-based max hold.
    max_hold = risk.get("maxHoldBars")
    if max_hold is not None and bars_held >= max_hold:
        return "Max hold"

    # 6. Rule-based exit — gated by a minimum hold so a noisy signal can't open and slam shut on the
    #    same/adjacent bar (the whipsaw that produced 0.00% round-trips live). Absent => 0 (no gate).
    min_hold = risk.get("minHoldBars")
    min_hold = 0 if min_hold is None else min_hold
    if bars_held >= min_hold and eval_condition(ir["exit"], s, cache, i):
        return "Exit rule"
    return None


def should_enter(ir: dict, candles: list[dict], i: int) -> bool:
    s = build_series(candles)
    return i >= warmup_bars(ir) and eval_condition(ir["entry"], s, {}, i)


def exit_reason(ir: dict, candles: list[dict], i: int, ctx: dict) -> Optional[str]:
    return _exit_decision(ir, build_series(candles), {}, i, ctx)


def simulate_from_ir(candles: list[dict], ir: dict) -> dict:
    trades: list[float] = []
    hold_bars: list[int] = []  # bars held per closed trade (parallel to `trades`), for the avg-hold gate
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)

    position = "NONE"
    entry_price = 0.0
    bars_held = 0
    peak_price = 0.0
    equity = 10000.0
    peak_equity = equity
    max_drawdown = 0.0
    current_dd_duration = 0
    max_dd_duration = 0

    if len(candles) <= warmup:
        return {"trades": trades, "holdBars": hold_bars, "maxDrawdown": 0.0, "drawdownDuration": 0}

    for i in range(warmup, len(candles)):
        price = s["close"][i]

        if equity > peak_equity:
            peak_equity = equity
            current_dd_duration = 0
        else:
            current_dd_duration += 1
            dd = ((peak_equity - equity) / peak_equity) * 100
            if dd > max_drawdown:
                max_drawdown = dd
            if current_dd_duration > max_dd_duration:
                max_dd_duration = current_dd_duration

        if position == "NONE":
            if eval_condition(ir["entry"], s, cache, i):
                position = "LONG"
                entry_price = price * 1.0005
                bars_held = 0
                peak_price = price
        else:
            bars_held += 1
            if price > peak_price:
                peak_price = price
            reason = _exit_decision(ir, s, cache, i,
                                    {"entryPrice": entry_price, "barsHeld": bars_held, "peakPrice": peak_price})
            if reason:
                exit_price = price * 0.9995
                net_return_pct = (exit_price - entry_price) / entry_price - 0.0015
                equity += equity * net_return_pct
                trades.append(net_return_pct)
                hold_bars.append(bars_held)
                position = "NONE"

    return {"trades": trades, "holdBars": hold_bars, "maxDrawdown": max_drawdown, "drawdownDuration": max_dd_duration}


def intended_position_state(ir: dict, candles: list[dict]) -> str:
    """The strategy's CURRENT intended position at the latest bar, from the same stateful replay the
    backtest uses (entry edge in, rule/risk exit out). The live loop reconciles real exposure to this
    rather than only reacting to a fresh entry edge, so activating (or restarting mid-trade) a
    strategy that is already signalling long opens the position to MATCH it instead of sitting flat
    until the next crossover. Mirrors interpreter.ts::intendedPositionState. Returns 'LONG' or 'NONE'."""
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)
    if len(candles) <= warmup:
        return "NONE"

    position = "NONE"
    entry_price = 0.0
    bars_held = 0
    peak_price = 0.0
    for i in range(warmup, len(candles)):
        price = s["close"][i]
        if not math.isfinite(price):
            continue
        if position == "NONE":
            if eval_condition(ir["entry"], s, cache, i):
                position = "LONG"
                entry_price = price
                bars_held = 0
                peak_price = price
        else:
            bars_held += 1
            if price > peak_price:
                peak_price = price
            if _exit_decision(ir, s, cache, i,
                              {"entryPrice": entry_price, "barsHeld": bars_held, "peakPrice": peak_price}):
                position = "NONE"
    return position


def generate_signals_from_ir(candles: list[dict], ir: dict) -> list[dict]:
    signals: list[dict] = []
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)
    times = [math.floor(int(c["timestamp"]) / 1000) for c in candles]

    position = "NONE"
    entry_price = 0.0
    bars_held = 0
    peak_price = 0.0
    for i in range(warmup, len(candles)):
        price = s["close"][i]
        if not math.isfinite(price):
            continue
        if position == "NONE":
            if eval_condition(ir["entry"], s, cache, i):
                position = "LONG"
                entry_price = price
                bars_held = 0
                peak_price = price
                signals.append({"time": times[i], "side": "buy", "price": price, "reason": "Entry rule"})
        else:
            bars_held += 1
            if price > peak_price:
                peak_price = price
            reason = _exit_decision(ir, s, cache, i,
                                    {"entryPrice": entry_price, "barsHeld": bars_held, "peakPrice": peak_price})
            if reason:
                position = "NONE"
                signals.append({"time": times[i], "side": "sell", "price": price, "reason": reason})
    return signals
