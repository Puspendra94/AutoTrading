"""Strategy IR helpers: warmup lookback, overfitting-budget parameter count, semantic validation,
and legacy indicatorConfig auto-translate.

The IR is plain JSON (dicts/lists) since it arrives from the DB/LLM as parametersJson. This is the
single engine now (the TypeScript twin was deleted in the Phase B consolidation).
"""
from __future__ import annotations

import json
from typing import Any

from .indicators import CANDLESTICK_PATTERNS

PERIOD_MIN = 2
PERIOD_MAX = 400
COMPARISON_OPS = {"gt", "lt", "gte", "lte", "crossAbove", "crossBelow"}

# A 'both' strategy trades LONG and SHORT from one rule tree: `entry`/`exit` are the long side and
# `shortEntry`/`shortExit` the short side, with a trend filter picking which one can fire. Written
# symmetrically (same indicator periods, mirrored thresholds) the shared operands de-duplicate in
# count_ir_parameters, so it costs ~1-2 parameters over a single-sided strategy rather than double.
BOTH = "both"
DIRECTIONS = ("long", "short", BOTH)


def trade_sides(ir: dict) -> list[tuple[str, dict, dict]]:
    """[(direction, entry_tree, exit_tree), ...] — one entry for a single-sided strategy, two for
    'both'. The single place that knows how a direction maps to its trees."""
    direction = ir.get("direction") or "long"
    if direction == BOTH:
        return [
            ("long", ir["entry"], ir["exit"]),
            ("short", ir.get("shortEntry") or ir["entry"], ir.get("shortExit") or ir["exit"]),
        ]
    return [(direction, ir["entry"], ir["exit"])]


# ---- Warmup ----
def _operand_lookback(o: dict) -> int:
    if o.get("op") != "indicator":
        return 0
    kind = o.get("kind")
    if kind in ("ema", "sma", "bollinger", "donchian", "rollingHigh", "rollingLow"):
        return int(o["period"])
    if kind in ("rsi", "atr"):
        return int(o["period"]) + 1
    if kind == "adx":
        return 2 * int(o["period"]) + 1  # DI smoothing (period) + ADX smoothing (period)
    if kind == "candlestick":
        return 12  # multi-bar patterns (morning/evening star, 3-soldiers) look back a few bars
    if kind == "macd":
        return int(o["slow"]) + int(o["signal"])
    if kind == "stochastic":
        return int(o["period"]) + int(o["smoothK"]) + int(o["smoothD"])
    return 0


def _condition_lookback(c: dict) -> int:
    op = c.get("op")
    if op in ("and", "or"):
        return max([0] + [_condition_lookback(sub) for sub in c["conditions"]])
    if op == "not":
        return _condition_lookback(c["condition"])
    return max(_operand_lookback(c["left"]), _operand_lookback(c["right"]))


def warmup_bars(ir: dict) -> int:
    lookbacks = [1]
    for _, entry, exit_tree in trade_sides(ir):
        lookbacks += [_condition_lookback(entry), _condition_lookback(exit_tree)]
    return max(lookbacks)


# ---- Overfitting budget: count DISTINCT tunable knobs (mirror of countIRParameters) ----
def _operand_param_count(o: dict) -> int:
    if o.get("op") == "const":
        return 1
    if o.get("op") == "price":
        return 0
    kind = o.get("kind")
    if kind == "macd":
        return 3
    if kind == "bollinger":
        return 2
    if kind == "stochastic":
        return 3
    return 1


def _operand_key(o: dict) -> str:
    return json.dumps({k: v for k, v in o.items() if k != "field"}, sort_keys=True)


def _collect_operands(c: dict, into: dict) -> None:
    op = c.get("op")
    if op in ("and", "or"):
        for sub in c["conditions"]:
            _collect_operands(sub, into)
        return
    if op == "not":
        _collect_operands(c["condition"], into)
        return
    for o in (c["left"], c["right"]):
        if o.get("op") != "price":
            into[_operand_key(o)] = o


def count_ir_parameters(ir: dict) -> int:
    # Operands are keyed by shape, so an indicator reused across the long and short sides (the point
    # of a SYMMETRIC 'both' strategy) is charged once — only the mirrored thresholds cost extra.
    operands: dict = {}
    for _, entry, exit_tree in trade_sides(ir):
        _collect_operands(entry, operands)
        _collect_operands(exit_tree, operands)
    total = sum(_operand_param_count(o) for o in operands.values())
    risk = ir.get("risk", {})
    risk_count = 2 + (1 if risk.get("trailingStopPct") is not None else 0) + (1 if risk.get("maxHoldBars") is not None else 0)
    return total + risk_count


# System-level profit-protection policy (Phase 1 adaptive-exit redesign). resolve_ir injects these
# into every strategy's risk block when the field is absent, so a generated strategy can never ship
# without downside/whipsaw protection. Risk hygiene, not alpha knobs -> NOT counted by
# count_ir_parameters. MUST stay identical to EXIT_DEFAULTS in
# apps/backend/src/modules/strategy/dsl/strategy-ir.ts.
EXIT_DEFAULTS = {
    "takeProfitMode": "soft",     # ride past the target under a tight trail (no hard profit cap)
    "breakevenTriggerPct": 2.0,   # once peak gain >= 2%, arm the profit floor...
    "breakevenFloorPct": 0.0,     # ...so the trade can't fall back below breakeven (winner can't become loser)
    "minHoldBars": 2,             # rule-based exit can't fire before 2 bars (kills same-bar whipsaws)
    "postTargetTrailPct": 2.0,    # soft mode: trail 2% off the peak once the target is reached
}


def apply_exit_defaults(ir: dict) -> dict:
    """Fill in any unset profit-protection fields with the system defaults (non-destructive)."""
    risk = dict(ir.get("risk") or {})
    for key, default in EXIT_DEFAULTS.items():
        if risk.get(key) is None:
            risk[key] = default
    return {**ir, "risk": risk}


def resolve_ir(params: dict):
    """Resolve any params blob to a validated IR (legacy auto-translated, rule-tree validated, then
    system profit-protection defaults injected); returns None when the result isn't executable."""
    ir = legacy_to_ir(params) if is_legacy_params(params) else params
    return apply_exit_defaults(ir) if not validate_ir(ir) else None


def _collect_indicator_kinds(c: dict, into: set) -> None:
    op = c.get("op")
    if op in ("and", "or"):
        for sub in c["conditions"]:
            _collect_indicator_kinds(sub, into)
        return
    if op == "not":
        _collect_indicator_kinds(c["condition"], into)
        return
    for o in (c.get("left"), c.get("right")):
        if isinstance(o, dict) and o.get("op") == "indicator":
            into.add(o["kind"])


def strategy_type_tag(params: dict) -> str:
    """Coarse tag = sorted distinct indicator kinds across the tree (mirror of strategyTypeTag).
    Lenient: reads only the tree structure (no strategyName/risk requirement)."""
    ir = legacy_to_ir(params) if is_legacy_params(params) else params
    if not isinstance(ir, dict) or "entry" not in ir or "exit" not in ir:
        return "unclassified"
    kinds: set = set()
    try:
        for _, entry, exit_tree in trade_sides(ir):
            _collect_indicator_kinds(entry, kinds)
            _collect_indicator_kinds(exit_tree, kinds)
    except Exception:  # noqa: BLE001 — malformed tree -> unclassified
        return "unclassified"
    return "_".join(sorted(kinds)) if kinds else "price_action"


# ---- Semantic validation (mirrors validateOperand/validateCondition/validateIR) ----
def _is_period(p: Any) -> bool:
    return isinstance(p, int) and not isinstance(p, bool) and PERIOD_MIN <= p <= PERIOD_MAX


def validate_operand(o: Any, path: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(o, dict):
        return [f"{path}: operand must be an object"]
    op = o.get("op")
    if op == "const":
        v = o.get("value")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errs.append(f"{path}: const.value must be a number")
        return errs
    if op == "price":
        if o.get("field") not in ("open", "high", "low", "close", "volume"):
            errs.append(f"{path}: price.field invalid")
        return errs
    if op != "indicator":
        return [f"{path}: unknown operand op '{op}'"]
    kind = o.get("kind")
    if kind in ("ema", "sma", "rsi", "atr", "adx", "donchian", "rollingHigh", "rollingLow"):
        if not _is_period(o.get("period")):
            errs.append(f"{path}: {kind}.period must be an int in [{PERIOD_MIN},{PERIOD_MAX}]")
        if kind == "donchian" and o.get("field") not in ("upper", "lower"):
            errs.append(f"{path}: donchian.field must be upper|lower")
    elif kind == "macd":
        if not (_is_period(o.get("fast")) and _is_period(o.get("slow")) and _is_period(o.get("signal"))):
            errs.append(f"{path}: macd.fast/slow/signal must be ints in range")
        elif o["fast"] >= o["slow"]:
            errs.append(f"{path}: macd.fast must be < macd.slow")
        if o.get("field") not in ("line", "signal", "hist"):
            errs.append(f"{path}: macd.field must be line|signal|hist")
    elif kind == "bollinger":
        if not _is_period(o.get("period")):
            errs.append(f"{path}: bollinger.period out of range")
        m = o.get("mult")
        if not isinstance(m, (int, float)) or isinstance(m, bool) or m <= 0 or m > 5:
            errs.append(f"{path}: bollinger.mult must be in (0,5]")
        if o.get("field") not in ("upper", "mid", "lower"):
            errs.append(f"{path}: bollinger.field must be upper|mid|lower")
    elif kind == "stochastic":
        if not (_is_period(o.get("period")) and _is_period(o.get("smoothK")) and _is_period(o.get("smoothD"))):
            errs.append(f"{path}: stochastic.period/smoothK/smoothD out of range")
        if o.get("field") not in ("k", "d"):
            errs.append(f"{path}: stochastic.field must be k|d")
    elif kind == "candlestick":
        if o.get("pattern") not in CANDLESTICK_PATTERNS:
            errs.append(f"{path}: candlestick.pattern must be one of {sorted(CANDLESTICK_PATTERNS)}")
    else:
        errs.append(f"{path}: unknown indicator kind '{kind}'")
    return errs


def validate_condition(c: Any, path: str) -> list[str]:
    if not isinstance(c, dict):
        return [f"{path}: condition must be an object"]
    op = c.get("op")
    errs: list[str] = []
    if op in ("and", "or"):
        conds = c.get("conditions")
        if not isinstance(conds, list) or not conds:
            errs.append(f"{path}: {op} needs a non-empty conditions[]")
        else:
            for i, sub in enumerate(conds):
                errs += validate_condition(sub, f"{path}.{op}[{i}]")
    elif op == "not":
        errs += validate_condition(c.get("condition"), f"{path}.not")
    elif op in COMPARISON_OPS:
        errs += validate_operand(c.get("left"), f"{path}.left")
        errs += validate_operand(c.get("right"), f"{path}.right")
    else:
        errs.append(f"{path}: unknown condition op '{op}'")
    return errs


def _num_in(v: Any, lo: float, hi: float) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _int_in(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _validate_risk_extras(risk: dict) -> list[str]:
    """Validate the Phase 1 adaptive-exit fields (all optional). Ranges mirror the TS zod schema."""
    errs: list[str] = []
    if "trailingStopPct" in risk and risk["trailingStopPct"] is not None and not _num_in(risk["trailingStopPct"], 0.1, 50):
        errs.append("risk.trailingStopPct must be in [0.1,50]")
    if "maxHoldBars" in risk and risk["maxHoldBars"] is not None and not _int_in(risk["maxHoldBars"], 1, 5000):
        errs.append("risk.maxHoldBars must be an int in [1,5000]")
    if "takeProfitMode" in risk and risk["takeProfitMode"] is not None and risk["takeProfitMode"] not in ("hard", "soft"):
        errs.append("risk.takeProfitMode must be hard|soft")
    if "breakevenTriggerPct" in risk and risk["breakevenTriggerPct"] is not None and not _num_in(risk["breakevenTriggerPct"], 0.1, 50):
        errs.append("risk.breakevenTriggerPct must be in [0.1,50]")
    if "breakevenFloorPct" in risk and risk["breakevenFloorPct"] is not None and not _num_in(risk["breakevenFloorPct"], -20, 20):
        errs.append("risk.breakevenFloorPct must be in [-20,20]")
    if "minHoldBars" in risk and risk["minHoldBars"] is not None and not _int_in(risk["minHoldBars"], 0, 500):
        errs.append("risk.minHoldBars must be an int in [0,500]")
    if "postTargetTrailPct" in risk and risk["postTargetTrailPct"] is not None and not _num_in(risk["postTargetTrailPct"], 0.1, 50):
        errs.append("risk.postTargetTrailPct must be in [0.1,50]")
    return errs


def validate_ir(ir: Any) -> list[str]:
    if not isinstance(ir, dict):
        return ["ir must be an object"]
    errs: list[str] = []
    if not ir.get("strategyName"):
        errs.append("strategyName is required")
    if "direction" in ir and ir["direction"] is not None and ir["direction"] not in DIRECTIONS:
        errs.append(f"direction must be {'|'.join(DIRECTIONS)}")
    for key in ("entry", "exit"):
        if key not in ir:
            errs.append(f"{key} is required")
    if ir.get("direction") == BOTH:
        # A 'both' strategy is only meaningful with its own short side; without it the short leg
        # would silently reuse the long rules and trade backwards.
        for key in ("shortEntry", "shortExit"):
            if key not in ir:
                errs.append(f"{key} is required when direction is '{BOTH}'")
    risk = ir.get("risk")
    if not isinstance(risk, dict):
        errs.append("risk is required")
    else:
        sl, tp = risk.get("stopLossPct"), risk.get("takeProfitPct")
        if not isinstance(sl, (int, float)) or not (0.1 <= sl <= 20):
            errs.append("risk.stopLossPct must be in [0.1,20]")
        if not isinstance(tp, (int, float)) or not (0.1 <= tp <= 50):
            errs.append("risk.takeProfitPct must be in [0.1,50]")
        errs += _validate_risk_extras(risk)
    if errs:
        return errs
    for direction, entry, exit_tree in trade_sides(ir):
        label = "" if ir.get("direction") != BOTH else f"[{direction}] "
        errs += validate_condition(entry, f"{label}entry") + validate_condition(exit_tree, f"{label}exit")
    return errs


# ---- Legacy auto-translate (mirrors legacyToIR) ----
def legacy_to_ir(params: dict) -> dict:
    cfg = (params or {}).get("indicatorConfig") or {}
    fast = cfg.get("emaFastPeriod") or 12
    slow = cfg.get("emaSlowPeriod") or 26
    trend = cfg.get("trendEmaPeriod")
    entry_conds = [
        {"op": "crossAbove",
         "left": {"op": "indicator", "kind": "ema", "period": fast},
         "right": {"op": "indicator", "kind": "ema", "period": slow}},
    ]
    if trend:
        entry_conds.append({"op": "gt",
                            "left": {"op": "price", "field": "close"},
                            "right": {"op": "indicator", "kind": "ema", "period": trend}})
    return {
        "strategyName": (params or {}).get("strategyName") or "Legacy EMA Crossover",
        "reasoning": "Auto-translated from legacy indicatorConfig params.",
        "entry": entry_conds[0] if len(entry_conds) == 1 else {"op": "and", "conditions": entry_conds},
        "exit": {"op": "lt",
                 "left": {"op": "indicator", "kind": "ema", "period": fast},
                 "right": {"op": "indicator", "kind": "ema", "period": slow}},
        "risk": {"stopLossPct": cfg.get("stopLossPct") or 1.5, "takeProfitPct": cfg.get("takeProfitPct") or 3.5},
    }


def is_legacy_params(params: Any) -> bool:
    return bool(isinstance(params, dict) and params.get("indicatorConfig") and not params.get("entry"))
