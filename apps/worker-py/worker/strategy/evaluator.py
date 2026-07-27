"""Deterministic backtest & evaluation — faithful port of strategy-evaluator.service.ts.

Method-for-method with the backend so a strategy the worker generates clears (or fails) the
exact same gate the backend would apply. Verified by tools/parity_evaluator.py, which runs
identical candles/params/policy through the TS service and this port and diffs every field.

Determinism note: everything here is deterministic EXCEPT run_monte_carlo (trade-order
reshuffle uses RNG in both languages), so the three monteCarloSummary fields — passRate,
minSharpe, maxDrawdown — are stochastic and are excluded from exact parity. The evaluation
GATE does not depend on Monte Carlo, so the pass/fail decision is fully reproducible.

`params` is a plain dict shaped like StrategyParams (indicatorConfig.emaFastPeriod, ...).
`candles` is a list of dicts: {"close": float, "timestamp": int_ms}. `policy` is a dict with
minSharpe / maxDrawdownPct / minProfitFactor / minTradeCount / maxParameterCount.
"""
from __future__ import annotations

import math
import random
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from .dsl.interpreter import simulate_from_ir
from .dsl.ir import count_ir_parameters, resolve_ir

MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000


def to_fixed(x: float, digits: int) -> float:
    """Match JS `Number(x.toFixed(digits))`: round the EXACT double half toward +infinity
    (ties -> larger value), then return it as a float. Uses Decimal(x) — the exact binary
    value, not repr — so e.g. 1.005 rounds like V8 does."""
    d = Decimal(x)
    q = Decimal(1).scaleb(-digits)
    rounding = ROUND_HALF_UP if d >= 0 else ROUND_HALF_DOWN  # both = half toward +infinity
    return float(d.quantize(q, rounding=rounding))


def _num(v: Any) -> float:
    """Mirror TS `Number(x)` on policy fields that may arrive as strings/None -> NaN."""
    if v is None:
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _calculate_ema(prices: list[float], period: int) -> list[float]:
    ema = [0.0] * len(prices)
    if not prices:
        return ema
    k = 2 / (period + 1)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = prices[i] * k + ema[i - 1] * (1 - k)
    return ema


def _estimate_periods_per_year(candles: list[dict]) -> float:
    if len(candles) < 2:
        return 252.0  # fallback: assume daily bars
    total_ms = candles[-1]["timestamp"] - candles[0]["timestamp"]
    avg_ms_per_candle = total_ms / (len(candles) - 1)
    if avg_ms_per_candle <= 0:
        return 252.0
    return MS_PER_YEAR / avg_ms_per_candle


def _count_tunable_parameters(params: Any) -> int:
    ir = resolve_ir(params)
    return count_ir_parameters(ir) if ir else 0


def _simulate_trades(candles: list[dict], params: dict) -> dict:
    # One code path for every strategy: resolve to a rule tree (legacy params auto-translated)
    # and run the shared DSL interpreter (identical slippage/fee/equity accounting to the TS side).
    # Invalid params -> no trades. Mirrors strategy-evaluator.service.ts::simulateTrades.
    ir = resolve_ir(params)
    if not ir:
        return {"trades": [], "maxDrawdown": 0.0, "drawdownDuration": 0}
    return simulate_from_ir(candles, ir)


def _compute_metrics(sim: dict, periods_per_year: float) -> dict:
    trades = sim["trades"]
    trade_count = len(trades)
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (3.0 if gross_profit > 0 else 0)

    compounded_return = math.prod((1 + r) for r in trades) - 1 if trades else 0.0
    win_rate = (len(wins) / trade_count) * 100 if trade_count > 0 else 0

    mean_return = sum(trades) / trade_count if trade_count > 0 else 0
    std_dev = (
        math.sqrt(sum((n - mean_return) ** 2 for n in trades) / (trade_count - 1))
        if trade_count > 1
        else 0
    )
    downside_losses = [t for t in trades if t < 0]
    downside_std_dev = (
        math.sqrt(sum(n ** 2 for n in downside_losses) / len(downside_losses))
        if downside_losses
        else 0
    )

    trades_per_year = trade_count if (periods_per_year > 0 and trade_count > 0) else 0
    annualization_factor = math.sqrt(trades_per_year) if trades_per_year > 0 else 0
    sharpe = (mean_return / std_dev) * annualization_factor if std_dev > 0 else 0
    sortino = (mean_return / downside_std_dev) * annualization_factor if downside_std_dev > 0 else 0
    annualized_return = mean_return * trades_per_year
    calmar = (annualized_return * 100) / sim["maxDrawdown"] if sim["maxDrawdown"] > 0 else 0

    return {
        "sharpe": to_fixed(sharpe, 2),
        "sortino": to_fixed(sortino, 2),
        "calmar": to_fixed(calmar, 2),
        "profitFactor": to_fixed(profit_factor, 2),
        "tradeCount": trade_count,
        "totalReturnPct": to_fixed(compounded_return * 100, 2),
        "winRate": to_fixed(win_rate, 1),
    }


def _run_monte_carlo(trades: list[float], policy: dict) -> dict:
    RUNS = 100
    if len(trades) == 0:
        return {"passRate": 0, "minSharpe": 0, "maxDrawdown": 100}

    pass_count = 0
    run_sharpes: list[float] = []
    run_drawdowns: list[float] = []

    for _ in range(RUNS):
        shuffled = sorted(trades, key=lambda _t: random.random())  # trade-order reshuffle
        mc_equity = 10000.0
        mc_peak = mc_equity
        mc_dd = 0.0
        for r in shuffled:
            mc_equity += mc_equity * r
            if mc_equity > mc_peak:
                mc_peak = mc_equity
            dd = ((mc_peak - mc_equity) / mc_peak) * 100
            if dd > mc_dd:
                mc_dd = dd
        mean = sum(shuffled) / len(shuffled)
        std = (
            math.sqrt(sum((n - mean) ** 2 for n in shuffled) / (len(shuffled) - 1))
            if len(shuffled) > 1
            else 0
        )
        run_sharpes.append(mean / std if std > 0 else 0)
        run_drawdowns.append(mc_dd)
        if mc_dd <= _num(policy.get("maxDrawdownPct")):
            pass_count += 1

    run_sharpes.sort()
    run_drawdowns.sort()
    p5_sharpe = run_sharpes[math.floor(len(run_sharpes) * 0.05)]
    p95_drawdown = run_drawdowns[math.floor(len(run_drawdowns) * 0.95)]

    return {
        "passRate": to_fixed((pass_count / RUNS) * 100, 1),
        "minSharpe": to_fixed(p5_sharpe, 2),
        "maxDrawdown": to_fixed(p95_drawdown, 2),
    }


def _compute_regime_breakdown(candles: list[dict], params: dict, periods_per_year: float) -> dict:
    CHUNKS = 4
    chunk_size = len(candles) // CHUNKS
    if chunk_size < 20:
        full = _compute_metrics(_simulate_trades(candles, params), periods_per_year)
        return {"trending": full["sharpe"], "choppy": full["sharpe"], "highVol": full["sharpe"]}

    buckets: dict[str, list[float]] = {"trending": [], "choppy": [], "highVol": []}

    for c in range(CHUNKS):
        start = c * chunk_size
        end = len(candles) if c == CHUNKS - 1 else start + chunk_size
        chunk = candles[start:end]
        if len(chunk) < 20:
            continue

        closes = [float(x["close"]) for x in chunk]
        returns = [(closes[i + 1] - closes[i]) / closes[i] for i in range(len(closes) - 1)]
        mean_ret = sum(returns) / len(returns)
        volatility = math.sqrt(sum((r - mean_ret) ** 2 for r in returns) / len(returns))
        trend_slope = (closes[-1] - closes[0]) / closes[0]

        chunk_metrics = _compute_metrics(_simulate_trades(chunk, params), periods_per_year)

        if abs(trend_slope) > volatility * 2:
            buckets["trending"].append(chunk_metrics["sharpe"])
        elif volatility > 0.01:
            buckets["highVol"].append(chunk_metrics["sharpe"])
        else:
            buckets["choppy"].append(chunk_metrics["sharpe"])

    def avg(arr: list[float]) -> float:
        return sum(arr) / len(arr) if arr else 0

    return {
        "trending": to_fixed(avg(buckets["trending"]), 2),
        "choppy": to_fixed(avg(buckets["choppy"]), 2),
        "highVol": to_fixed(avg(buckets["highVol"]), 2),
    }


def evaluate_strategy(candles: list[dict], params: dict, policy: dict) -> dict:
    """Backtest & evaluate a strategy over OHLCV data. Returns the same shape as
    BacktestPerformanceMetrics (camelCase keys)."""
    empty_result = {
        "sharpe": 0, "sortino": 0, "calmar": 0, "maxDrawdown": 100, "drawdownDuration": 0,
        "profitFactor": 0, "tradeCount": 0, "totalReturnPct": 0, "winRate": 0,
        "monteCarloSummary": {
            "passRate": 0, "minSharpe": 0, "maxDrawdown": 100,
            "walkForward": {"inSampleTradeCount": 0, "outOfSampleTradeCount": 0, "inSampleSharpe": 0, "outOfSampleSharpe": 0},
        },
        "regimeBreakdown": {"trending": 0, "choppy": 0, "highVol": 0},
        "parameterCount": 0,
        "passedEvaluationGate": False,
    }

    if not candles or len(candles) < 50:
        return empty_result

    periods_per_year = _estimate_periods_per_year(candles)

    # Walk-forward split (spec 7.2): in-sample is never what's reported/gated on.
    split_idx = math.floor(len(candles) * 0.7)
    in_sample = candles[:split_idx]
    out_of_sample = candles[split_idx:]

    in_sample_sim = _simulate_trades(in_sample, params)
    out_of_sample_sim = _simulate_trades(out_of_sample, params)

    in_sample_metrics = _compute_metrics(in_sample_sim, periods_per_year)
    oos_metrics = _compute_metrics(out_of_sample_sim, periods_per_year)

    monte_carlo = _run_monte_carlo(out_of_sample_sim["trades"], policy)
    regime_breakdown = _compute_regime_breakdown(candles, params, periods_per_year)

    trade_count = oos_metrics["tradeCount"]
    passed_sharpe = oos_metrics["sharpe"] >= _num(policy.get("minSharpe"))
    passed_drawdown = out_of_sample_sim["maxDrawdown"] <= _num(policy.get("maxDrawdownPct"))
    passed_profit_factor = oos_metrics["profitFactor"] >= _num(policy.get("minProfitFactor"))
    # Honest, self-consistent trade-count gate: require the policy's minTradeCount directly (no
    # hidden ">=5" override, which used to make the policy lie to the LLM — its lessons kept
    # chasing "100 trades" when the real bar was 5). minTradeCount is tuned to what's actually
    # reachable on the eval interval so it stays meaningful without being impossible.
    passed_trade_count = trade_count >= _num(policy.get("minTradeCount"))

    parameter_count = _count_tunable_parameters(params)
    passed_parameter_count = parameter_count <= _num(policy.get("maxParameterCount"))

    passed_evaluation_gate = (
        passed_sharpe and passed_drawdown and passed_profit_factor and passed_trade_count and passed_parameter_count
    )

    return {
        "sharpe": oos_metrics["sharpe"],
        "sortino": oos_metrics["sortino"],
        "calmar": oos_metrics["calmar"],
        "maxDrawdown": to_fixed(out_of_sample_sim["maxDrawdown"], 2),
        "drawdownDuration": out_of_sample_sim["drawdownDuration"],
        "profitFactor": oos_metrics["profitFactor"],
        "tradeCount": trade_count,
        "totalReturnPct": oos_metrics["totalReturnPct"],
        "winRate": oos_metrics["winRate"],
        "monteCarloSummary": {
            **monte_carlo,
            "walkForward": {
                "inSampleTradeCount": len(in_sample_sim["trades"]),
                "outOfSampleTradeCount": len(out_of_sample_sim["trades"]),
                "inSampleSharpe": in_sample_metrics["sharpe"],
                "outOfSampleSharpe": oos_metrics["sharpe"],
            },
        },
        "regimeBreakdown": regime_breakdown,
        "parameterCount": parameter_count,
        "passedEvaluationGate": passed_evaluation_gate,
    }
