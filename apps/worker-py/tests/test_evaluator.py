"""Golden-value regression tests for the evaluator port (Phase 3b-2).

The golden numbers below were captured from the REAL backend StrategyEvaluatorService
(tools/parity_evaluator.py + apps/backend/tools/parity_evaluator_runner.ts) on the exact
same deterministic candles. They pin the Python port to the TypeScript output so a future
edit that drifts from the backend fails here — without needing Node/ts-node at test time.

Monte Carlo fields (passRate/minSharpe/maxDrawdown) are RNG-driven in both languages and are
intentionally not asserted; the evaluation GATE does not depend on them.
"""
import math

from worker.strategy.evaluator import evaluate_strategy, to_fixed

START_MS = 1_700_000_000_000
STEP_MS = 60_000

PARAMS = {
    "strategyName": "Parity Test",
    "indicatorConfig": {
        "emaFastPeriod": 12, "emaSlowPeriod": 26,
        "stopLossPct": 1.5, "takeProfitPct": 3.5,
    },
    "reasoning": "parity",
}
POLICY = {
    "minSharpe": 1.0, "maxDrawdownPct": 20.0, "minProfitFactor": 1.3,
    "minTradeCount": 100, "maxParameterCount": 5,
}


def _high_freq_candles(n: int = 900) -> list[dict]:
    # Must stay identical to tools/parity_evaluator.py::scenario_high_freq.
    closes = [120 + 6 * math.sin(i / 4.0) + 2 * math.sin(i / 1.3) + 0.02 * i for i in range(n)]
    return [{"close": c, "timestamp": START_MS + i * STEP_MS} for i, c in enumerate(closes)]


# Captured from the backend TS evaluator on _high_freq_candles() + PARAMS + POLICY.
# Regenerated after the Phase 1 adaptive-exit redesign: resolveIR now injects the system
# profit-protection defaults (soft take-profit + profit floor + 2-bar min-hold), so this legacy
# strategy is backtested through the SAME exit ladder that runs live. On this choppy oscillating
# series "let profits run" underperforms the old hard 3.5% take-profit (Sharpe 0.96 -> -1.07) — the
# gate correctly still rejects it. Both engines produce these numbers (proven by parity_evaluator.py).
GOLDEN = {
    "sharpe": -1.07, "sortino": -0.85, "calmar": -0.68, "maxDrawdown": 7.86,
    "drawdownDuration": 244, "profitFactor": 0.46, "tradeCount": 10,
    "totalReturnPct": -5.33, "winRate": 50, "parameterCount": 4,
    "passedEvaluationGate": False,
    "regimeBreakdown": {"trending": -0.18, "choppy": 0, "highVol": -1.98},
    "walkForward": {
        "inSampleTradeCount": 24, "outOfSampleTradeCount": 10,
        "inSampleSharpe": -2.47, "outOfSampleSharpe": -1.07,
    },
}


def test_evaluator_matches_backend_golden():
    result = evaluate_strategy(_high_freq_candles(), PARAMS, POLICY)
    for field in (
        "sharpe", "sortino", "calmar", "maxDrawdown", "drawdownDuration",
        "profitFactor", "tradeCount", "totalReturnPct", "winRate",
        "parameterCount", "passedEvaluationGate",
    ):
        assert result[field] == GOLDEN[field], f"{field}: got {result[field]}, expected {GOLDEN[field]}"
    assert result["regimeBreakdown"] == GOLDEN["regimeBreakdown"]
    assert result["monteCarloSummary"]["walkForward"] == GOLDEN["walkForward"]


def test_relaxed_policy_flips_gate_true():
    # Same metrics, lax policy -> gate passes. Proves the gate=True path.
    relaxed = {"minSharpe": -100.0, "maxDrawdownPct": 100.0, "minProfitFactor": 0.0,
               "minTradeCount": 1, "maxParameterCount": 20}
    result = evaluate_strategy(_high_freq_candles(), PARAMS, relaxed)
    assert result["passedEvaluationGate"] is True


def test_trend_filter_only_removes_entries():
    # The trend-regime filter can only SUPPRESS long entries (price must be above the trend EMA),
    # never add them — so a filtered run has <= the trades of the unfiltered one, and adds the
    # trendEmaPeriod knob to the parameter count.
    candles = _high_freq_candles()
    base = evaluate_strategy(candles, PARAMS, POLICY)
    filtered_params = {
        "strategyName": "Trend Filtered",
        "indicatorConfig": {**PARAMS["indicatorConfig"], "trendEmaPeriod": 100},
        "reasoning": "trend",
    }
    filtered = evaluate_strategy(candles, filtered_params, POLICY)
    assert filtered["parameterCount"] == 5
    assert base["parameterCount"] == 4
    assert filtered["tradeCount"] <= base["tradeCount"]
    # On this rising series the filter should still leave at least one trade (it's not a no-op ban).
    assert filtered["tradeCount"] >= 1


def test_too_few_candles_returns_empty():
    result = evaluate_strategy([{"close": 100, "timestamp": START_MS}] * 10, PARAMS, POLICY)
    assert result["passedEvaluationGate"] is False
    assert result["maxDrawdown"] == 100
    assert result["tradeCount"] == 0


def test_to_fixed_matches_js_semantics():
    assert to_fixed(1.005, 2) == 1.0        # exact double is 1.00499… -> rounds down, like V8
    assert to_fixed(2.5, 0) == 3.0          # positive tie -> up
    assert to_fixed(0.145, 2) == 0.14       # exact double 0.1449… -> down
    assert to_fixed(-2.675, 2) == -2.67     # negative, exact double -2.6749… -> toward +inf
