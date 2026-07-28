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
from .ir import trade_sides, warmup_bars


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
    elif kind == "adx":
        arr = ind.adx(s, o["period"])
    elif kind == "candlestick":
        arr = ind.candlestick(s, o["pattern"])
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


# ---- Direction-aware fills / P&L / extreme (Phase 4). SHORT sells to open, buys to close. ----
def _entry_fill(direction: str, price: float) -> float:
    return price * (0.9995 if direction == "short" else 1.0005)


def _exit_fill(direction: str, price: float) -> float:
    return price * (1.0005 if direction == "short" else 0.9995)


def _net_return(direction: str, entry_price: float, exit_price: float) -> float:
    gross = (entry_price - exit_price) / entry_price if direction == "short" else (exit_price - entry_price) / entry_price
    return gross - 0.0015  # 0.15% round-trip fee


def _better_extreme(direction: str, extreme: float, price: float) -> float:
    """Track the favorable extreme: trough (min) for a short, peak (max) for a long."""
    if direction == "short":
        return price if price < extreme else extreme
    return price if price > extreme else extreme


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


def _exit_decision(ir: dict, s: dict, cache: dict, i: int, ctx: dict,
                   direction: Optional[str] = None, exit_tree: Optional[dict] = None) -> Optional[str]:
    """Adaptive exit ladder (Phase 1) — direction-aware (Phase 4). Deterministic and fully
    backtestable; a live AI overlay (Phase 2) may only TIGHTEN this, never loosen it. `extremePrice`
    is the peak-since-entry for a long, the trough-since-entry for a short. Mirrors exitDecision in
    interpreter.ts.

    `direction`/`exit_tree` name the side being closed. They default to the strategy's own single
    direction and `exit` tree; a 'both' strategy passes the side it currently holds, so a short leg
    is judged with short P&L and closed by the SHORT exit rules."""
    price = s["close"][i]
    entry_price = ctx["entryPrice"]
    bars_held = ctx["barsHeld"]
    extreme_price = ctx["extremePrice"]
    direction = direction or ir.get("direction") or "long"
    exit_tree = ir["exit"] if exit_tree is None else exit_tree
    is_short = direction == "short"
    if is_short:
        # Short profits when price FALLS; the favorable extreme is the trough, and the trailing
        # stop is triggered by an adverse RISE off that trough.
        return_pct = (entry_price - price) / entry_price
        extreme_return = (entry_price - extreme_price) / entry_price if extreme_price > 0 else return_pct
        adverse_from_extreme = (price - extreme_price) / extreme_price if extreme_price > 0 else 0.0
    else:
        return_pct = (price - entry_price) / entry_price
        extreme_return = (extreme_price - entry_price) / entry_price if extreme_price > 0 else return_pct
        adverse_from_extreme = (extreme_price - price) / extreme_price if extreme_price > 0 else 0.0
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
        if extreme_return >= be_trigger / 100 and return_pct <= be_floor / 100:
            return "Profit floor"

    # 3. Take-profit target. hard = book immediately; soft = ride past it under a tight trail (#4).
    #    Absent mode => 'hard' (legacy behavior for raw IRs).
    tp_mode = risk.get("takeProfitMode") or "hard"
    if return_pct >= risk["takeProfitPct"] / 100 and tp_mode == "hard":
        return "Take profit"

    # 4. Trailing stop off the favorable extreme. The strategy's own trail (if any) is always active;
    #    in soft-TP mode a tighter post-target trail kicks in once the extreme has reached the target.
    eff_trail = risk.get("trailingStopPct")
    if tp_mode == "soft" and extreme_return >= risk["takeProfitPct"] / 100:
        post_trail = risk.get("postTargetTrailPct")
        post_trail = 2.0 if post_trail is None else post_trail
        eff_trail = post_trail if eff_trail is None else min(eff_trail, post_trail)
    if eff_trail is not None and extreme_price > 0:
        if adverse_from_extreme >= eff_trail / 100:
            return "Trailing stop"

    # 5. Time-based max hold.
    max_hold = risk.get("maxHoldBars")
    if max_hold is not None and bars_held >= max_hold:
        return "Max hold"

    # 6. Rule-based exit — gated by a minimum hold so a noisy signal can't open and slam shut on the
    #    same/adjacent bar (the whipsaw that produced 0.00% round-trips live). Absent => 0 (no gate).
    min_hold = risk.get("minHoldBars")
    min_hold = 0 if min_hold is None else min_hold
    if bars_held >= min_hold and eval_condition(exit_tree, s, cache, i):
        return "Exit rule"
    return None


def should_enter(ir: dict, candles: list[dict], i: int) -> bool:
    """True when ANY side's entry fires on bar i (a 'both' strategy has two)."""
    return entry_side(ir, candles, i) is not None


def entry_side(ir: dict, candles: list[dict], i: int) -> Optional[str]:
    """Which direction ('long'/'short') wants to open on bar i, or None. The live loop needs the
    side — not just a yes/no — to open the correct leg of a 'both' strategy."""
    if i < warmup_bars(ir):
        return None
    s = build_series(candles)
    cache: dict = {}
    for side_direction, entry_tree, _ in trade_sides(ir):
        if eval_condition(entry_tree, s, cache, i):
            return side_direction
    return None


def exit_reason(ir: dict, candles: list[dict], i: int, ctx: dict,
                direction: Optional[str] = None) -> Optional[str]:
    """`direction` is the side actually held — required for a 'both' strategy so the position is
    judged with the right P&L sign and closed by that side's exit rules."""
    exit_tree = None
    if direction:
        for side_direction, _, side_exit in trade_sides(ir):
            if side_direction == direction:
                exit_tree = side_exit
                break
    return _exit_decision(ir, build_series(candles), {}, i, ctx, direction=direction, exit_tree=exit_tree)


def simulate_from_ir(candles: list[dict], ir: dict) -> dict:
    trades: list[float] = []
    hold_bars: list[int] = []  # bars held per closed trade (parallel to `trades`), for the avg-hold gate
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)

    # One side for a long/short strategy, two for 'both'. When flat, sides are tested in order and
    # the first whose entry fires is opened; a symmetric strategy's trend filter keeps them mutually
    # exclusive, and if both somehow fire the long is taken (never both at once — the risk gate caps
    # a ticker at one open position anyway).
    sides = trade_sides(ir)
    position = "NONE"          # 'NONE' | 'long' | 'short' — the side currently held
    open_exit_tree: dict = {}
    entry_price = 0.0
    bars_held = 0
    extreme_price = 0.0
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
            for side_direction, entry_tree, exit_tree in sides:
                if eval_condition(entry_tree, s, cache, i):
                    position = side_direction
                    open_exit_tree = exit_tree
                    entry_price = _entry_fill(side_direction, price)
                    bars_held = 0
                    extreme_price = price
                    break
        else:
            bars_held += 1
            extreme_price = _better_extreme(position, extreme_price, price)
            reason = _exit_decision(
                ir, s, cache, i,
                {"entryPrice": entry_price, "barsHeld": bars_held, "extremePrice": extreme_price},
                direction=position, exit_tree=open_exit_tree,
            )
            if reason:
                exit_price = _exit_fill(position, price)
                net_return_pct = _net_return(position, entry_price, exit_price)
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
    until the next crossover. Mirrors interpreter.ts::intendedPositionState. Returns the direction it
    should currently be holding ('LONG'/'SHORT') or 'NONE'."""
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)
    if len(candles) <= warmup:
        return "NONE"

    sides = trade_sides(ir)
    position = "NONE"   # 'NONE' | 'LONG' | 'SHORT'
    held_direction = "long"
    open_exit_tree: dict = {}
    entry_price = 0.0
    bars_held = 0
    extreme_price = 0.0
    for i in range(warmup, len(candles)):
        price = s["close"][i]
        if not math.isfinite(price):
            continue
        if position == "NONE":
            for side_direction, entry_tree, exit_tree in sides:
                if eval_condition(entry_tree, s, cache, i):
                    position = "SHORT" if side_direction == "short" else "LONG"
                    held_direction = side_direction
                    open_exit_tree = exit_tree
                    entry_price = price
                    bars_held = 0
                    extreme_price = price
                    break
        else:
            bars_held += 1
            extreme_price = _better_extreme(held_direction, extreme_price, price)
            if _exit_decision(ir, s, cache, i,
                              {"entryPrice": entry_price, "barsHeld": bars_held, "extremePrice": extreme_price},
                              direction=held_direction, exit_tree=open_exit_tree):
                position = "NONE"
    return position


def generate_signals_from_ir(candles: list[dict], ir: dict) -> list[dict]:
    signals: list[dict] = []
    s = build_series(candles)
    cache: dict = {}
    warmup = warmup_bars(ir)
    times = [math.floor(int(c["timestamp"]) / 1000) for c in candles]

    # Each marker carries the direction of the leg that produced it, so a 'both' strategy's chart can
    # label a long entry BUY and a short entry SHORT on the same series (a single strategy-level
    # direction can't express that).
    sides = trade_sides(ir)
    position = "NONE"
    held_direction = "long"
    open_exit_tree: dict = {}
    entry_price = 0.0
    bars_held = 0
    extreme_price = 0.0
    for i in range(warmup, len(candles)):
        price = s["close"][i]
        if not math.isfinite(price):
            continue
        if position == "NONE":
            for side_direction, entry_tree, exit_tree in sides:
                if eval_condition(entry_tree, s, cache, i):
                    position = "OPEN"
                    held_direction = side_direction
                    open_exit_tree = exit_tree
                    entry_price = price
                    bars_held = 0
                    extreme_price = price
                    signals.append({
                        "time": times[i],
                        "side": "sell" if side_direction == "short" else "buy",  # a short opens by selling
                        "direction": side_direction, "price": price, "reason": "Entry rule",
                    })
                    break
        else:
            bars_held += 1
            extreme_price = _better_extreme(held_direction, extreme_price, price)
            reason = _exit_decision(ir, s, cache, i,
                                    {"entryPrice": entry_price, "barsHeld": bars_held, "extremePrice": extreme_price},
                                    direction=held_direction, exit_tree=open_exit_tree)
            if reason:
                position = "NONE"
                signals.append({
                    "time": times[i],
                    "side": "buy" if held_direction == "short" else "sell",  # ...and closes by buying back
                    "direction": held_direction, "price": price, "reason": reason,
                })
    return signals
