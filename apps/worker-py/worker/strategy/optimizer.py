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


def _substitute(node: Any, assignment: dict) -> Any:
    """Deep-copy a template node, replacing every {"$param": name} leaf with assignment[name]."""
    if isinstance(node, dict):
        if set(node.keys()) == {"$param"}:
            return assignment[node["$param"]]
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
        out.append((assignment, _substitute(base, assignment)))
    return out


def optimize(template: dict, candles: list[dict], policy: dict) -> dict:
    """Grid-search `template` on in-sample, robust-select, then validate the winner walk-forward.
    Returns {ir, evaluation, assignment, tried, inSampleSharpe} — ir/evaluation are None if the
    template yielded nothing scoreable."""
    combos = expand_template(template)
    empty = {"ir": None, "evaluation": None, "assignment": None, "tried": 0, "inSampleSharpe": None}
    if not combos or len(candles) < 50:
        return empty

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
