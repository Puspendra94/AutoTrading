"""Parity harness for the evaluator port (Phase 3b-2).

Generates deterministic candle scenarios, runs each through BOTH the backend TS evaluator
(via ts-node) and the Python port, and diffs every deterministic field. Monte Carlo fields
(passRate/minSharpe/maxDrawdown) are RNG-driven in both languages and are excluded.

    python tools/parity_evaluator.py          # from apps/worker-py, with backend deps installed

Exit code 0 = all deterministic fields matched; 1 = a mismatch (prints the offending field).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.strategy.evaluator import evaluate_strategy  # noqa: E402

BACKEND_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "backend")
)
START_MS = 1_700_000_000_000
STEP_MS = 60_000  # 1m candles

# Fields compared exactly (deterministic). Monte Carlo passRate/minSharpe/maxDrawdown skipped.
FLAT_FIELDS = [
    "sharpe", "sortino", "calmar", "maxDrawdown", "drawdownDuration",
    "profitFactor", "tradeCount", "totalReturnPct", "winRate",
    "parameterCount", "passedEvaluationGate",
]


def _candles(closes: list[float]) -> list[dict]:
    return [{"close": c, "timestamp": START_MS + i * STEP_MS} for i, c in enumerate(closes)]


def scenario_trending_up(n: int = 900) -> list[float]:
    # Upward drift with fast oscillation so many EMA crosses + SL/TP fire.
    return [100 + 0.04 * i + 3.5 * math.sin(i / 5.0) + 1.2 * math.sin(i / 1.7) for i in range(n)]


def scenario_choppy(n: int = 900) -> list[float]:
    # Range-bound, no net trend, frequent reversals.
    return [100 + 5 * math.sin(i / 6.0) + 2.5 * math.cos(i / 2.3) for i in range(n)]


def scenario_downtrend(n: int = 900) -> list[float]:
    return [180 - 0.05 * i + 4 * math.sin(i / 5.5) + 1.4 * math.cos(i / 1.9) for i in range(n)]


def scenario_high_freq(n: int = 900) -> list[float]:
    # Strong, steady oscillation designed to generate a long trade series and a healthy PF.
    return [120 + 6 * math.sin(i / 4.0) + 2 * math.sin(i / 1.3) + 0.02 * i for i in range(n)]


SCENARIOS = {
    "trending_up": scenario_trending_up(),
    "choppy": scenario_choppy(),
    "downtrend": scenario_downtrend(),
    "high_freq": scenario_high_freq(),
}

PARAMS = {
    "strategyName": "Parity Test",
    "indicatorConfig": {
        "emaFastPeriod": 12,
        "emaSlowPeriod": 26,
        "rsiPeriod": 14,
        "rsiBuyThreshold": 45,
        "rsiSellThreshold": 65,
        "stopLossPct": 1.5,
        "takeProfitPct": 3.5,
    },
    "reasoning": "parity",
}

POLICY = {
    "minSharpe": 1.0,
    "maxDrawdownPct": 20.0,
    "minProfitFactor": 1.3,
    "minTradeCount": 100,
    "maxParameterCount": 5,
}

# Deliberately lax so at least one scenario clears the gate — proves the gate=True path
# matches too, not just the reject path.
RELAXED_POLICY = {
    "minSharpe": -100.0,
    "maxDrawdownPct": 100.0,
    "minProfitFactor": 0.0,
    "minTradeCount": 1,
    "maxParameterCount": 20,
}


def run_ts(payload: dict) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        path = f.name
    try:
        out = subprocess.run(
            ["npx", "ts-node", "-T", "tools/parity_evaluator_runner.ts", path],
            cwd=BACKEND_DIR, capture_output=True, text=True, check=True,
        )
        return json.loads(out.stdout)
    finally:
        os.unlink(path)


def compare(name: str, ts: dict, py: dict) -> list[str]:
    diffs = []
    for field in FLAT_FIELDS:
        if ts[field] != py[field]:
            diffs.append(f"[{name}] {field}: TS={ts[field]} PY={py[field]}")
    for bucket in ("trending", "choppy", "highVol"):
        if ts["regimeBreakdown"][bucket] != py["regimeBreakdown"][bucket]:
            diffs.append(f"[{name}] regimeBreakdown.{bucket}: TS={ts['regimeBreakdown'][bucket]} PY={py['regimeBreakdown'][bucket]}")
    wf_ts, wf_py = ts["monteCarloSummary"]["walkForward"], py["monteCarloSummary"]["walkForward"]
    for field in ("inSampleTradeCount", "outOfSampleTradeCount", "inSampleSharpe", "outOfSampleSharpe"):
        if wf_ts[field] != wf_py[field]:
            diffs.append(f"[{name}] walkForward.{field}: TS={wf_ts[field]} PY={wf_py[field]}")
    return diffs


def main() -> int:
    all_diffs: list[str] = []
    for policy_name, policy in (("strict", POLICY), ("relaxed", RELAXED_POLICY)):
        for name, closes in SCENARIOS.items():
            candles = _candles(closes)
            payload = {"candles": candles, "params": PARAMS, "policy": policy}
            ts = run_ts(payload)
            py = evaluate_strategy(candles, PARAMS, policy)
            diffs = compare(name, ts, py)
            status = "OK" if not diffs else "MISMATCH"
            label = f"{name}/{policy_name}"
            print(f"{label:24s} trades(oos)={ts['tradeCount']:3d} gate={ts['passedEvaluationGate']!s:5s} "
                  f"sharpe={ts['sharpe']} -> {status}")
            all_diffs.extend(diffs)

    if all_diffs:
        print("\nDETERMINISTIC MISMATCHES:")
        for d in all_diffs:
            print("  " + d)
        return 1
    print("\nAll deterministic fields matched across all scenarios. ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
