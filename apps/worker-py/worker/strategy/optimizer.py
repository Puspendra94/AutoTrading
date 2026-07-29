"""Two-stage generation, Stage 2: parameter optimization over a strategy TEMPLATE.

Stage 1 (the LLM) proposes a SHAPE — a rule tree whose tunable numbers are {"$param": name}
markers plus a `search` dict of candidate values per name. Stage 2 (here) expands the grid,
scores every config on the IN-SAMPLE window ONLY (never the held-out fold — that would leak),
robustly picks the best, and validates it walk-forward through the gate. No guessing: it measures.

Overfitting discipline lives here: selection touches only in-sample; the out-of-sample gate is the
independent judge; the grid is capped; a min-trade floor kills tiny flukes.
"""
from __future__ import annotations

import copy
import itertools
import logging
import random
from math import floor
from typing import Any, Optional

from .dsl.ir import resolve_ir
from .dsl.interpreter import simulate_from_ir
from .evaluator import _compute_metrics, _estimate_periods_per_year, evaluate_strategy

log = logging.getLogger("worker.strategy.optimizer")

MAX_GRID = 800          # cap the search — more configs = more noise-fitting + compute
IN_SAMPLE_FRAC = 0.7    # must match evaluate_strategy's walk-forward split
BOTH = "both"
# How far a two-sided strategy's oscillator entry levels may drift from a true mirror. Perfectly
# mirrored levels sum to 100 (30/70, 35/65). A promoted strategy used 30/70's lopsided cousin —
# long at RSI<30 but short at RSI>60 — which made the short leg far easier to trigger: over one
# 539-bar window the short leg fired 10 times and the long leg ZERO. 5 points keeps 30/65 while
# rejecting 30/60.
MIRROR_TOLERANCE = 3.0
# Mirroring alone is not enough: a perfectly mirrored 30/70 pair is still too EXTREME to fire. The
# promoted 30/65 strategy produced two long entries in seven months — technically two-sided, useless
# in practice. The planner's own prompt already says "RSI below ~35 (NOT below 30: below 30 fires
# too rarely)"; nothing enforced it, so the model used 30 anyway. These bands make it enforceable —
# and combined with mirroring they permit 35/65, 40/60, 45/55 while rejecting 30/70.
LONG_LEVEL_BAND = (33.0, 47.0)
SHORT_LEVEL_BAND = (53.0, 67.0)


class TemplateError(ValueError):
    """The LLM's template is internally inconsistent (e.g. references an undeclared $param).
    Raised so the caller can discard that ATTEMPT instead of the whole generation job."""


def _substitute(node: Any, assignment: dict) -> Any:
    """Deep-copy a template node, replacing every {"$param": name} leaf with assignment[name]."""
    if isinstance(node, dict):
        if set(node.keys()) == {"$param"}:
            name = node["$param"]
            if name not in assignment:
                # Seen in the wild: the tree used {"$param":"overbought"} while `search` only
                # declared other names. This raised a bare KeyError out of optimize() and killed the
                # entire generation run — all remaining attempts included.
                raise TemplateError(f"template references undeclared search parameter '{name}'")
            return assignment[name]
        return {k: _substitute(v, assignment) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(x, assignment) for x in node]
    return node


def expand_template(template: dict, cap: int = MAX_GRID, seed: int = 0) -> list[tuple[dict, dict]]:
    """Return [(assignment, concrete_ir), ...] for every combo in template['search'] (capped)."""
    search = template.get("search") or {}
    base = {k: v for k, v in template.items() if k != "search"}
    if not search:
        return [({}, copy.deepcopy(base))]
    names = list(search.keys())
    combos = list(itertools.product(*(search[n] for n in names)))
    if len(combos) > cap:
        combos = random.Random(seed).sample(combos, cap)  # deterministic sub-sample
    out = []
    for combo in combos:
        assignment = dict(zip(names, combo))
        out.append((assignment, _substitute(base, assignment)))  # TemplateError -> caller discards
    return out


def oscillator_level(tree: Any) -> Optional[float]:
    """The RSI/Stochastic threshold an entry tree compares against, or None if it uses no oscillator."""
    found: list[float] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("op") in ("gt", "lt", "gte", "lte", "crossAbove", "crossBelow"):
            left, right = node.get("left"), node.get("right")
            if (isinstance(left, dict) and left.get("op") == "indicator"
                    and (left.get("kind") or "").lower() in ("rsi", "stochastic")
                    and isinstance(right, dict) and right.get("op") == "const"
                    and isinstance(right.get("value"), (int, float)) and not isinstance(right.get("value"), bool)):
                found.append(float(right["value"]))
        for v in node.values():
            if isinstance(v, dict):
                walk(v)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

    walk(tree)
    return found[0] if found else None


def level_problem(ir: dict) -> Optional[str]:
    """Why a two-sided strategy's entry levels are unusable, or None if they're fine.

    Two independent requirements, both learned the hard way:
      * MIRRORED — 30/60 made the short leg fire far more often than the long.
      * TRADEABLE — even a perfect 30/70 mirror is too extreme to fire; that shape produced two
        long entries in seven months."""
    if (ir.get("direction") or "") != BOTH:
        return None
    long_level = oscillator_level(ir.get("entry"))
    short_level = oscillator_level(ir.get("shortEntry"))
    if long_level is None or short_level is None:
        return None  # not an oscillator pair — nothing to check
    if abs((long_level + short_level) - 100.0) > MIRROR_TOLERANCE:
        return (f"entry levels {long_level:g}/{short_level:g} are not mirrored "
                f"(sum {long_level + short_level:g}, need 100±{MIRROR_TOLERANCE:g})")
    if not (LONG_LEVEL_BAND[0] <= long_level <= LONG_LEVEL_BAND[1]):
        return (f"long entry level {long_level:g} is outside the tradeable band "
                f"{LONG_LEVEL_BAND[0]:g}-{LONG_LEVEL_BAND[1]:g} — it would fire too rarely")
    if not (SHORT_LEVEL_BAND[0] <= short_level <= SHORT_LEVEL_BAND[1]):
        return (f"short entry level {short_level:g} is outside the tradeable band "
                f"{SHORT_LEVEL_BAND[0]:g}-{SHORT_LEVEL_BAND[1]:g} — it would fire too rarely")
    return None


def is_mirrored(ir: dict) -> bool:
    """True when a two-sided strategy's entry levels are both mirrored AND frequent enough to trade."""
    return level_problem(ir) is None


def optimize(template: dict, candles: list[dict], policy: dict) -> dict:
    """Grid-search `template` on in-sample, robust-select, then validate the winner walk-forward.
    Returns {ir, evaluation, assignment, tried, inSampleSharpe} — ir/evaluation are None if the
    template yielded nothing scoreable."""
    combos = expand_template(template)
    empty = {"ir": None, "evaluation": None, "assignment": None, "tried": 0, "inSampleSharpe": None}
    if not combos or len(candles) < 50:
        return empty

    # For a two-sided strategy, keep only genuinely mirrored level pairs. The template can be written
    # symmetrically and STILL come out lopsided, because the grid varies the long and short levels
    # independently — the search is what picks the final numbers. Filtering here also shrinks the
    # grid, which is a bonus against overfitting.
    if (template.get("direction") or "") == BOTH:
        mirrored = [(a, ir) for a, ir in combos if is_mirrored(ir)]
        if mirrored:
            log.info("Optimizer: two-sided strategy — %d/%d configs have mirrored entry levels.",
                     len(mirrored), len(combos))
            combos = mirrored
        else:
            # Better a lopsided candidate the gate can still reject than no candidate at all.
            log.warning("Optimizer: no mirrored level pair in the grid (%d configs); the long and "
                        "short legs will fire at different rates.", len(combos))

    split = floor(len(candles) * IN_SAMPLE_FRAC)
    in_sample = candles[:split]
    ppy = _estimate_periods_per_year(in_sample)
    # Require ENOUGH in-sample trades that the (smaller) out-of-sample fold will still clear the gate's
    # minTradeCount — scale by the in-sample:out-of-sample ratio. This stops the search from picking an
    # over-strict, rarely-firing config that then fails the OOS trade count.
    oos_frac = 1 - IN_SAMPLE_FRAC
    min_trades = max(int(policy.get("minTradeCount") or 8),
                     round((policy.get("minTradeCount") or 8) * IN_SAMPLE_FRAC / oos_frac))

    scored: list[tuple[float, dict, dict]] = []  # (in-sample sharpe, assignment, ir)
    for assignment, ir in combos:
        resolved = resolve_ir(ir)
        if not resolved:
            continue
        sim = simulate_from_ir(in_sample, resolved)
        m = _compute_metrics(sim, ppy)
        if m["tradeCount"] < min_trades:      # not enough in-sample trades to trust
            continue
        scored.append((m["sharpe"], assignment, ir))

    if not scored:
        log.info("Optimizer: %d configs tried, none met the in-sample trade floor (%d).", len(combos), min_trades)
        return {**empty, "tried": len(combos)}

    # Robust pick: best in-sample Sharpe. (A plateau-vs-needle refinement can be layered on later;
    # the out-of-sample gate below is the real overfit guard regardless.)
    scored.sort(key=lambda x: x[0], reverse=True)
    best_sharpe, best_assignment, best_ir = scored[0]

    # Judge the winner on the FULL series — evaluate_strategy re-splits the same 70/30 and gates on
    # the held-out fold the optimizer never used for selection.
    evaluation = evaluate_strategy(candles, best_ir, policy)
    log.info("Optimizer: %d scoreable configs; best in-sample Sharpe %.2f -> OOS gate %s (%s).",
             len(scored), best_sharpe, "PASS" if evaluation["passedEvaluationGate"] else "fail", best_assignment)
    return {"ir": best_ir, "evaluation": evaluation, "assignment": best_assignment,
            "tried": len(scored), "inSampleSharpe": best_sharpe}
