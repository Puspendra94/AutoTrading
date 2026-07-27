"""Parity harness for the DSL interpreter (Phase 2).

Generates deterministic OHLC candle scenarios and a battery of rule trees (one per indicator
family), runs each through BOTH the backend TS interpreter (via ts-node, simulateFromIR) and the
Python port (simulate_from_ir), and diffs the resulting trade series + drawdown. Because the
indicator math is mirrored operation-for-operation, results should be IEEE-754 identical; a tiny
tolerance is allowed on trade returns but the trade COUNT and drawdownDuration must match exactly
(a differing count means a signal decision diverged).

    python tools/parity_dsl.py     # from apps/worker-py, backend deps installed

Exit 0 = all trees matched across all scenarios; 1 = a mismatch (printed).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.strategy.dsl.interpreter import simulate_from_ir  # noqa: E402
from worker.strategy.dsl.ir import legacy_to_ir, validate_ir  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
START_MS = 1_700_000_000_000
STEP_MS = 3_600_000  # 1h candles
TOL = 1e-9


def _ohlc(closes: list[float], seed: int = 7) -> list[dict]:
    """Build deterministic OHLC candles from a close series (seeded jitter for high/low)."""
    state = seed
    def rand() -> float:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF
    candles = []
    prev = closes[0]
    for i, close in enumerate(closes):
        op = prev
        hi = max(op, close) + rand() * 0.8
        lo = min(op, close) - rand() * 0.8
        candles.append({
            "open": op, "high": hi, "low": lo, "close": close,
            "volume": 100 + rand() * 20, "timestamp": START_MS + i * STEP_MS,
        })
        prev = close
    return candles


def scenario_trending_up(n=900):
    return [100 + 0.04 * i + 3.5 * math.sin(i / 5.0) + 1.2 * math.sin(i / 1.7) for i in range(n)]


def scenario_choppy(n=900):
    return [100 + 5 * math.sin(i / 6.0) + 2.5 * math.cos(i / 2.3) for i in range(n)]


def scenario_downtrend(n=900):
    return [180 - 0.05 * i + 4 * math.sin(i / 5.5) + 1.4 * math.cos(i / 1.9) for i in range(n)]


def scenario_high_freq(n=900):
    return [120 + 6 * math.sin(i / 4.0) + 2 * math.sin(i / 1.3) + 0.02 * i for i in range(n)]


SCENARIOS = {
    "trending_up": _ohlc(scenario_trending_up()),
    "choppy": _ohlc(scenario_choppy()),
    "downtrend": _ohlc(scenario_downtrend()),
    "high_freq": _ohlc(scenario_high_freq()),
}


def ema(p, period):
    return {"op": "indicator", "kind": "ema", "period": period}


def _rsi(period=14):
    return {"op": "indicator", "kind": "rsi", "period": period}


def const(v):
    return {"op": "const", "value": v}


def close():
    return {"op": "price", "field": "close"}


# One rule tree per indicator family — exercises every operand/condition path.
IRS = {
    "legacy_crossover": legacy_to_ir({
        "indicatorConfig": {"emaFastPeriod": 12, "emaSlowPeriod": 26, "trendEmaPeriod": 200, "stopLossPct": 1.5, "takeProfitPct": 3.5},
    }),
    "sma_cross": {
        "strategyName": "SMA cross", "reasoning": "",
        "entry": {"op": "crossAbove", "left": {"op": "indicator", "kind": "sma", "period": 10}, "right": {"op": "indicator", "kind": "sma", "period": 30}},
        "exit": {"op": "crossBelow", "left": {"op": "indicator", "kind": "sma", "period": 10}, "right": {"op": "indicator", "kind": "sma", "period": 30}},
        "risk": {"stopLossPct": 2, "takeProfitPct": 4},
    },
    "rsi_meanrev": {
        "strategyName": "RSI dip", "reasoning": "",
        "entry": {"op": "lt", "left": _rsi(14), "right": const(40)},
        "exit": {"op": "gt", "left": _rsi(14), "right": const(60)},
        "risk": {"stopLossPct": 3, "takeProfitPct": 6},
    },
    "macd_hist": {
        "strategyName": "MACD hist", "reasoning": "",
        "entry": {"op": "crossAbove", "left": {"op": "indicator", "kind": "macd", "fast": 12, "slow": 26, "signal": 9, "field": "line"},
                  "right": {"op": "indicator", "kind": "macd", "fast": 12, "slow": 26, "signal": 9, "field": "signal"}},
        "exit": {"op": "lt", "left": {"op": "indicator", "kind": "macd", "fast": 12, "slow": 26, "signal": 9, "field": "hist"}, "right": const(0)},
        "risk": {"stopLossPct": 2.5, "takeProfitPct": 5},
    },
    "bollinger_breakout": {
        "strategyName": "BB breakout", "reasoning": "",
        "entry": {"op": "crossAbove", "left": close(), "right": {"op": "indicator", "kind": "bollinger", "period": 20, "mult": 2, "field": "upper"}},
        "exit": {"op": "lt", "left": close(), "right": {"op": "indicator", "kind": "bollinger", "period": 20, "mult": 2, "field": "mid"}},
        "risk": {"stopLossPct": 3, "takeProfitPct": 6},
    },
    "donchian_channel": {
        # Buy when the bar's low touches the 20-bar channel bottom (built from lows), exit when
        # the high touches the channel top. Compares matching price fields so it actually fires.
        "strategyName": "Donchian channel", "reasoning": "",
        "entry": {"op": "lte", "left": {"op": "price", "field": "low"}, "right": {"op": "indicator", "kind": "donchian", "period": 20, "field": "lower"}},
        "exit": {"op": "gte", "left": {"op": "price", "field": "high"}, "right": {"op": "indicator", "kind": "donchian", "period": 20, "field": "upper"}},
        "risk": {"stopLossPct": 4, "takeProfitPct": 8},
    },
    "stochastic": {
        "strategyName": "Stoch", "reasoning": "",
        "entry": {"op": "lt", "left": {"op": "indicator", "kind": "stochastic", "period": 14, "smoothK": 3, "smoothD": 3, "field": "k"}, "right": const(30)},
        "exit": {"op": "gt", "left": {"op": "indicator", "kind": "stochastic", "period": 14, "smoothK": 3, "smoothD": 3, "field": "k"}, "right": const(70)},
        "risk": {"stopLossPct": 2, "takeProfitPct": 4},
    },
    "atr_trailing_maxhold": {
        "strategyName": "ATR + trailing + maxhold", "reasoning": "",
        "entry": {"op": "crossAbove", "left": ema(None, 9), "right": ema(None, 21)},
        "exit": {"op": "gt", "left": {"op": "indicator", "kind": "atr", "period": 14}, "right": const(6)},
        "risk": {"stopLossPct": 3, "takeProfitPct": 20, "trailingStopPct": 2.5, "maxHoldBars": 40},
    },
    "not_or_rollinghl": {
        # Buy dips to the 15-bar low; exit on a 15-bar high OR when price falls back under EMA50
        # (exercises the or/not condition ops and rollingHigh/rollingLow operands).
        "strategyName": "rolling H/L with not/or", "reasoning": "",
        "entry": {"op": "lte", "left": {"op": "price", "field": "low"}, "right": {"op": "indicator", "kind": "rollingLow", "period": 15}},
        "exit": {"op": "or", "conditions": [
            {"op": "gte", "left": {"op": "price", "field": "high"}, "right": {"op": "indicator", "kind": "rollingHigh", "period": 15}},
            {"op": "not", "condition": {"op": "gt", "left": close(), "right": ema(None, 50)}},
        ]},
        "risk": {"stopLossPct": 5, "takeProfitPct": 10},
    },
    "soft_tp_profit_floor": {
        # Phase 1 adaptive exit: soft take-profit (ride past 8% under a 2% post-target trail),
        # profit floor (peak >=2.5% then back below breakeven -> exit), and a 3-bar min-hold gate on
        # the rule exit. Exercises every new branch of the exit ladder in BOTH engines.
        "strategyName": "soft TP + profit floor", "reasoning": "",
        "entry": {"op": "crossAbove", "left": ema(None, 9), "right": ema(None, 21)},
        "exit": {"op": "crossBelow", "left": ema(None, 9), "right": ema(None, 21)},
        "risk": {
            "stopLossPct": 4, "takeProfitPct": 8, "takeProfitMode": "soft",
            "breakevenTriggerPct": 2.5, "breakevenFloorPct": 0, "minHoldBars": 3,
            "postTargetTrailPct": 2,
        },
    },
    "hard_tp_minhold": {
        # Hard take-profit + explicit min-hold + breakeven floor with a small negative buffer.
        "strategyName": "hard TP + minhold", "reasoning": "",
        "entry": {"op": "lt", "left": _rsi(14), "right": const(35)},
        "exit": {"op": "gt", "left": _rsi(14), "right": const(65)},
        "risk": {
            "stopLossPct": 3, "takeProfitPct": 5, "takeProfitMode": "hard",
            "breakevenTriggerPct": 3, "breakevenFloorPct": -0.5, "minHoldBars": 4,
        },
    },
}


def run_ts(candles, ir) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"candles": candles, "ir": ir}, f)
        path = f.name
    try:
        out = subprocess.run(
            ["npx", "ts-node", "-T", "tools/parity_dsl_runner.ts", path],
            cwd=BACKEND_DIR, capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)
    finally:
        os.unlink(path)


def compare(name, ts, py) -> list[str]:
    diffs = []
    if len(ts["trades"]) != len(py["trades"]):
        diffs.append(f"[{name}] trade COUNT: TS={len(ts['trades'])} PY={len(py['trades'])}")
        return diffs  # counts differ -> a decision diverged; per-trade diff is noise
    for i, (a, b) in enumerate(zip(ts["trades"], py["trades"])):
        if abs(a - b) > TOL:
            diffs.append(f"[{name}] trade[{i}]: TS={a} PY={b}")
    if ts.get("holdBars") != py.get("holdBars"):
        diffs.append(f"[{name}] holdBars: TS={ts.get('holdBars')} PY={py.get('holdBars')}")
    if abs(ts["maxDrawdown"] - py["maxDrawdown"]) > TOL:
        diffs.append(f"[{name}] maxDrawdown: TS={ts['maxDrawdown']} PY={py['maxDrawdown']}")
    if ts["drawdownDuration"] != py["drawdownDuration"]:
        diffs.append(f"[{name}] drawdownDuration: TS={ts['drawdownDuration']} PY={py['drawdownDuration']}")
    return diffs


def main() -> int:
    # First: every tree must be structurally valid.
    for name, ir in IRS.items():
        errs = validate_ir(ir)
        if errs:
            print(f"INVALID IR [{name}]: {errs}")
            return 1

    all_diffs: list[str] = []
    max_trades_per_ir: dict[str, int] = {}
    for ir_name, ir in IRS.items():
        for sc_name, candles in SCENARIOS.items():
            ts = run_ts(candles, ir)
            py = simulate_from_ir(candles, ir)
            diffs = compare(f"{ir_name}/{sc_name}", ts, py)
            status = "OK" if not diffs else "MISMATCH"
            print(f"{ir_name:22s} {sc_name:12s} trades={len(py['trades']):3d} -> {status}")
            all_diffs.extend(diffs)
            max_trades_per_ir[ir_name] = max(max_trades_per_ir.get(ir_name, 0), len(py["trades"]))

    # Guard against vacuous parity: a tree that never trades in any scenario proves nothing.
    vacuous = [name for name, mx in max_trades_per_ir.items() if mx == 0]
    if vacuous:
        print(f"\nVACUOUS (0 trades in every scenario — parity meaningless): {vacuous}")
        return 1

    if all_diffs:
        print("\nMISMATCHES:")
        for d in all_diffs:
            print("  " + d)
        return 1
    print("\nAll rule trees matched TS↔PY across all scenarios (each traded ≥1). ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
