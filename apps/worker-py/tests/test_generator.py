"""Tests for the strategy generation orchestration (Phase 3b-3).

Pure helpers are asserted directly; the gate-retry-promote control flow is exercised with a
fake store + fake LLM against the REAL (parity-verified) evaluator, so the flow is pinned to
generateStrategyForTicker's behavior without a DB or network.
"""
import dataclasses
import math

import worker.strategy.generator as gmod
from worker.config import config as worker_config
from worker.llm.schemas import StrategyGen, StrategyTemplate
from worker.strategy import generator as gen
from worker.strategy.generator import (
    StrategyGenerator,
    format_lessons_for_prompt,
    infer_strategy_type,
)
from worker.strategy.grammar_prompt import build_generation_prompt

STRICT = {"minSharpe": 1.0, "maxDrawdownPct": 20.0, "minProfitFactor": 1.3, "minTradeCount": 100, "maxParameterCount": 6}
RELAXED = {"minSharpe": -100.0, "maxDrawdownPct": 100.0, "minProfitFactor": 0.0, "minTradeCount": 1, "maxParameterCount": 20}

# A valid DSL rule tree (EMA crossover) the fake LLM returns. Knobs: ema12 + ema26 + SL + TP = 4.
GEN_TREE = {
    "strategyName": "EMA cross test",
    "reasoning": "test",
    "direction": "long",  # required: an omitted direction used to silently default and invert shorts
    "entry": {"op": "crossAbove", "left": {"op": "indicator", "kind": "ema", "period": 12},
              "right": {"op": "indicator", "kind": "ema", "period": 26}},
    "exit": {"op": "crossBelow", "left": {"op": "indicator", "kind": "ema", "period": 12},
             "right": {"op": "indicator", "kind": "ema", "period": 26}},
    "risk": {"stopLossPct": 1.5, "takeProfitPct": 3.5},
}


def _high_freq_candles(n: int = 900) -> list[dict]:
    closes = [120 + 6 * math.sin(i / 4.0) + 2 * math.sin(i / 1.3) + 0.02 * i for i in range(n)]
    return [{"close": c, "timestamp": 1_700_000_000_000 + i * 60_000} for i, c in enumerate(closes)]


# ------------------------------------------------------------------ pure helpers
def test_infer_strategy_type():
    # Tags are now the distinct indicator kinds in the (auto-translated) rule tree. A legacy
    # EMA-crossover blob translates to EMA-only indicators -> "ema" (the old rsiPeriod field is
    # not simulated and is dropped by the translation).
    assert infer_strategy_type({"indicatorConfig": {"emaFastPeriod": 12, "rsiPeriod": 14}}) == "ema"
    assert infer_strategy_type({"indicatorConfig": {"emaFastPeriod": 12}}) == "ema"
    # A rule tree using RSI + EMA tags as "ema_rsi".
    assert infer_strategy_type({
        "entry": {"op": "lt", "left": {"op": "indicator", "kind": "rsi", "period": 14}, "right": {"op": "const", "value": 30}},
        "exit": {"op": "crossBelow", "left": {"op": "indicator", "kind": "ema", "period": 10}, "right": {"op": "indicator", "kind": "ema", "period": 30}},
        "risk": {"stopLossPct": 2, "takeProfitPct": 4},
    }) == "ema_rsi"
    assert infer_strategy_type({"indicatorConfig": {}}) == "unclassified"
    assert infer_strategy_type(None) == "unclassified"


def test_format_lessons_for_prompt():
    assert "No prior lessons" in format_lessons_for_prompt([])
    out = format_lessons_for_prompt([
        {"outcome": "failure", "summaryText": "avoid tight stops"},
        {"outcome": "success", "summaryText": "wide TP helped"},
    ])
    assert out == "1. [FAILURE] avoid tight stops\n2. [SUCCESS] wide TP helped"


def test_build_generation_prompt_dsl_shape():
    p = build_generation_prompt("BTCUSDT", "1h", 120.0, param_budget=6, allow_short=False,
                                signal_emphasis="prefer RSI mean-reversion", lessons_block="No prior lessons yet.")
    assert p.startswith("Design an automated trading strategy for BTCUSDT (Interval: 1h, Latest Price: 120).")
    assert "LONG-ONLY" in p  # spot -> long only
    assert "MARKET TYPE: SPOT" in p
    assert "Parameter budget for THIS strategy: at most 6" in p
    assert "prefer RSI mean-reversion" in p  # planner emphasis threaded through


def test_build_generation_prompt_futures_allows_short():
    p = build_generation_prompt("BTCUSDT", "1h", 120.0, param_budget=6, allow_short=True,
                                signal_emphasis="", lessons_block="none")
    assert "MARKET TYPE: FUTURES" in p and '"direction":"short"' in p


# ------------------------------------------------------------------ fakes
class FakeLlm:
    def __init__(self, tree: dict):
        self.tree = tree

    async def generate_structured_completion(self, prompt, schema, schema_name="x", max_tokens=1024):
        # The planner only calls the LLM when there are lessons; the fake store returns none, so
        # every call here is a generation call -> return the DSL rule tree.
        return {"data": StrategyGen.model_validate(self.tree), "model": "test-model",
                "provider": "direct_api", "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}

    async def generate_completion(self, prompt, max_tokens=1024):
        return {"content": "distilled lesson", "model": "test-model", "provider": "direct_api",
                "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}

    async def log_cost(self, *a, **k):
        return None


class FakeStore:
    def __init__(self, policy: dict, live=None, blocking=False):
        self.policy = policy
        self.live = live
        self.blocking = blocking
        self.calls: list[str] = []
        self.stages: list[str] = []
        self.inserted_strategy = None
        self.inserted_backtest = None
        self.lessons_inserted: list[dict] = []

    async def get_ticker(self, tid):
        return {"id": tid, "symbol": "BTCUSDT", "interval": "1m", "market_type_name": "spot"}

    async def has_unresolved_blocking_flags(self, tid):
        return self.blocking

    async def set_onboarding_stage(self, tid, stage):
        self.stages.append(stage)

    async def load_recent_candles(self, tid, limit=2000):
        return _high_freq_candles()

    async def load_candles_for_interval(self, tid, interval, limit):
        return _high_freq_candles()

    async def get_latest_strategy_params(self, tid):
        return None

    async def get_active_policy(self):
        return self.policy

    async def count_strategies(self, tid):
        return 0

    async def insert_strategy(self, *, ticker_id, version, parameters_json, generated_by, eval_interval=None):
        self.calls.append("insert_strategy")
        self.inserted_strategy = {"version": version, "params": parameters_json,
                                  "generated_by": generated_by, "eval_interval": eval_interval}
        return "new-sid"

    async def insert_backtest(self, *, strategy_id, ev):
        self.calls.append("insert_backtest")
        self.inserted_backtest = ev

    async def get_live_strategy(self, tid):
        return self.live

    async def get_backtest_metrics(self, sid):
        return {"sharpe": 0.5, "profitFactor": 1.1, "maxDrawdown": 12.0, "passedEvaluationGate": True}

    async def get_latest_divergence(self, sid):
        return None

    async def retire_live(self, tid):
        self.calls.append("retire_live")

    async def promote_strategy(self, sid):
        self.calls.append("promote_strategy")

    async def set_ticker_active_ready(self, tid):
        self.calls.append("set_ticker_active_ready")

    async def retrieve_lessons(self, tid, strategy_type, limit=5):
        return []

    async def insert_lesson(self, **kw):
        self.calls.append("insert_lesson")
        self.lessons_inserted.append(kw)


# ------------------------------------------------------------------ flow
async def test_generate_persists_and_promotes_when_gate_passes():
    store = FakeStore(RELAXED)
    result = await StrategyGenerator(store, FakeLlm(GEN_TREE)).generate("tick-1", "cycle")
    assert result["saved"] is True
    assert result["attempts"] == 1
    assert store.inserted_strategy["version"] == 1
    # ema12 + ema26 + stopLoss + takeProfit = 4 distinct knobs.
    assert store.inserted_backtest["parameterCount"] == 4
    assert store.inserted_strategy["eval_interval"]  # the planned interval is persisted
    assert "insert_strategy" in store.calls and "insert_backtest" in store.calls
    assert store.calls.index("promote_strategy") > store.calls.index("insert_strategy")
    assert "set_ticker_active_ready" in store.calls
    assert store.stages[-1] == gen.STAGE_EVALUATING  # last stage set before promote


async def test_generate_fails_gate_persists_nothing():
    store = FakeStore(STRICT)  # minTradeCount 100 is unreachable on the fixture -> fails all 3 attempts
    result = await StrategyGenerator(store, FakeLlm(GEN_TREE)).generate("tick-1", "cycle")
    assert result["saved"] is False
    assert result["attempts"] == gen.MAX_ATTEMPTS
    assert "insert_strategy" not in store.calls
    assert store.stages[-1] == gen.STAGE_FAILED
    assert result["failingConditions"]  # names the exact failing condition(s)
    # A failed cycle still teaches the next one: a FAILURE lesson is recorded, with no source
    # strategy (nothing was persisted under the gate-before-save contract).
    assert "insert_lesson" in store.calls
    lesson = store.lessons_inserted[0]
    assert lesson["outcome"] == "failure"
    assert lesson["source_strategy_id"] is None
    assert lesson["strategy_type"] == "ema"  # the EMA rule tree tags as 'ema'


async def test_generate_skip_gate_promotes_failing_strategy():
    store = FakeStore(STRICT)  # would fail the gate...
    result = await StrategyGenerator(store, FakeLlm(GEN_TREE)).generate("tick-1", "cycle", skip_gate=True)
    assert result["saved"] is True and result["gateBypassed"] is True  # ...but skip_gate promotes it
    assert "promote_strategy" in store.calls


async def test_generate_rejects_short_on_spot():
    short_tree = {**GEN_TREE, "direction": "short"}
    store = FakeStore(RELAXED)  # fake get_ticker returns market_type_name='spot'
    result = await StrategyGenerator(store, FakeLlm(short_tree)).generate("tick-1", "cycle")
    assert result["saved"] is False  # short rejected on spot across all attempts
    assert "insert_strategy" not in store.calls


async def test_generate_rejects_direction_mismatch():
    """A short-shaped entry (enter while RSI is OVERBOUGHT, price BELOW its trend EMA) declared as a
    LONG must be discarded, not traded inverted — the exact bug that promoted a 'Short Mean-Reversion'
    strategy the engine then executed as a long (profit factor 1.37 -> 0.70)."""
    inverted = {
        **GEN_TREE, "direction": "long", "strategyName": "short rules mislabelled long",
        "entry": {"op": "and", "conditions": [
            {"op": "gt", "left": {"op": "indicator", "kind": "rsi", "period": 7},
             "right": {"op": "const", "value": 75}},
            {"op": "lt", "left": {"op": "price", "field": "close"},
             "right": {"op": "indicator", "kind": "ema", "period": 200}}]},
    }
    store = FakeStore(RELAXED)
    result = await StrategyGenerator(store, FakeLlm(inverted)).generate("tick-1", "cycle")
    assert result["saved"] is False
    assert "insert_strategy" not in store.calls


async def test_generate_retires_previous_live_and_records_lesson():
    live = {"id": "old-sid", "version": 1, "parametersJson": {"indicatorConfig": {"emaFastPeriod": 10}}}
    store = FakeStore(RELAXED, live=live)
    await StrategyGenerator(store, FakeLlm(GEN_TREE)).generate("tick-1", "divergence 40%")
    # Lesson recorded, previous retired, both BEFORE promotion of the new strategy.
    assert "insert_lesson" in store.calls
    assert store.calls.index("retire_live") < store.calls.index("promote_strategy")
    assert store.lessons_inserted[0]["source_strategy_id"] == "old-sid"


class FakeLlmTemplate:
    """Returns a strategy TEMPLATE (Stage 1 output) for the two-stage path."""
    def __init__(self, template: dict):
        self.template = template

    async def generate_structured_completion(self, prompt, schema, schema_name="x", max_tokens=1024):
        return {"data": StrategyTemplate.model_validate(self.template), "model": "test-model",
                "provider": "direct_api", "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}

    async def generate_completion(self, prompt, max_tokens=1024):
        return {"content": "x", "model": "t", "provider": "p", "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}

    async def log_cost(self, *a, **k):
        return None


# RSI(7) mean-reversion template with a small search grid — fires often on the oscillatory fixture.
RSI_TEMPLATE = {
    "strategyName": "RSI template", "reasoning": "", "direction": "long",
    "search": {"os": [30, 40], "rec": [55, 65]},
    "entry": {"op": "lt", "left": {"op": "indicator", "kind": "rsi", "period": 7},
              "right": {"op": "const", "value": {"$param": "os"}}},
    "exit": {"op": "gt", "left": {"op": "indicator", "kind": "rsi", "period": 7},
             "right": {"op": "const", "value": {"$param": "rec"}}},
    "risk": {"stopLossPct": 2, "takeProfitPct": 4},
}


async def test_two_stage_generation_optimizes_and_promotes(monkeypatch):
    # Flip the module's config to two-stage; the LLM returns a SHAPE and the real optimizer searches it.
    monkeypatch.setattr(gmod, "config", dataclasses.replace(worker_config, two_stage_generation=True))
    store = FakeStore(RELAXED)
    result = await StrategyGenerator(store, FakeLlmTemplate(RSI_TEMPLATE)).generate("tick-1", "cycle")
    assert result["saved"] is True
    assert "insert_strategy" in store.calls
    # The promoted strategy is a CONCRETE tree (the optimizer substituted the $param markers).
    params = store.inserted_strategy["params"]
    assert "$param" not in str(params)
    assert isinstance(params["exit"]["right"]["value"], (int, float))  # a real number, not a marker


async def test_generate_refuses_on_blocking_flags():
    store = FakeStore(RELAXED, blocking=True)
    try:
        await StrategyGenerator(store, FakeLlm(GEN_TREE)).generate("tick-1", "cycle")
        assert False, "expected refusal"
    except ValueError as e:
        assert "data quality" in str(e)
    assert store.stages[-1] == gen.STAGE_FAILED
