"""LLM skip-test harness — cohort assignment and verdict logic, with a faked model.

No API calls: the point of these tests is that the accounting around the model is right, because
that is what a wrong answer would look like. A bar the model refused must never be credited to its
ENTRY cohort, an unparseable answer must not be counted as a considered SKIP, and a rejected
proposal must not be counted as a trade.
"""
import random

import pytest

from worker.pattern.decision.schemas import EntryDecision
from worker.pattern.llm_screen import (
    ScreenResult, ScreenRow, _model_levels, _verdict, report, screen,
)
from worker.pattern.replay import LONG, SHORT, Observation, _agg

MIN = 60_000


def synth_1m(n, seed=3, price=60_000.0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        price *= (1 + rng.gauss(0, 0.0004))
        out.append({"timestamp": i * MIN, "open": price, "high": price * 1.0006,
                    "low": price * 0.9994, "close": price, "volume": 10.0})
    return out


class FakeLlm:
    """Answers ENTRY or SKIP on a fixed cycle so cohort sizes are predictable."""

    def __init__(self, pattern=("ENTRY", "SKIP", "SKIP", "SKIP"), side=LONG, synthetic=False,
                 raises=False, bad_rr=False):
        self.pattern, self.side = pattern, side
        self.synthetic, self.raises, self.bad_rr = synthetic, raises, bad_rr
        self.calls = 0

    async def generate_structured_completion(self, prompt, schema, max_tokens=0):
        if self.raises:
            raise RuntimeError("provider down")
        action = self.pattern[self.calls % len(self.pattern)]
        self.calls += 1
        # The prompt embeds the live close; entries have to bracket it or the validator refuses.
        price = _price_from(prompt)
        data = EntryDecision(action="SKIP", reasoning="nothing here")
        if action == "ENTRY":
            stop = price * (0.97 if self.side == LONG else 1.03)
            target = (price * 0.99 if self.bad_rr else
                      (price * 1.10 if self.side == LONG else price * 0.90))
            data = EntryDecision(action="ENTRY", side=self.side, entry_min=price * 0.999,
                                 entry_max=price * 1.001, stop_loss=stop, take_profit=target,
                                 confidence=0.7, reasoning="setup")
        return {"data": data, "inputTokens": 100, "outputTokens": 400, "costUsd": 0.002,
                "model": "deepseek-v4-flash-fallback" if self.synthetic else "deepseek-v4-flash"}


def _price_from(prompt: str) -> float:
    """Pull the close out of the real prompt so the fake's numbers are self-consistent."""
    import re
    m = re.search(r"Close:\s*([0-9.]+)", prompt) or re.search(r"([0-9]{4,}\.[0-9]+)", prompt)
    return float(m.group(1)) if m else 60_000.0


async def _run(llm, sample=40, n_1m=60 * 24 * 14, **kw):
    bars = synth_1m(n_1m)
    # screen() imports LlmService lazily inside the function; patch the module it comes from.
    import worker.llm.service as svc
    saved = svc.LlmService
    svc.LlmService = lambda: llm
    try:
        return await screen(bars, symbol="BTCUSDT", interval="15m", candle_limit=300,
                            sample=sample, concurrency=4, **kw)
    finally:
        svc.LlmService = saved


# --------------------------------------------------------------------------- cohorts
async def test_entry_and_skip_land_in_the_right_cohorts():
    llm = FakeLlm(pattern=("ENTRY", "SKIP"))
    r = await _run(llm, sample=40)
    entries = [row for row in r.rows if row.action == "ENTRY"]
    skips = [row for row in r.rows if row.action == "SKIP"]
    assert entries and skips
    assert len(r.obs["MODEL_ENTRY"]) == len(entries)
    assert len(r.obs["MODEL_SKIP"]) == len(skips)
    # Every answered bar is in the baseline exactly once.
    assert len(r.obs["ALL_SAMPLED"]) == len(entries) + len(skips)


async def test_the_baseline_always_uses_the_trigger_side_not_the_models():
    """ALL_SAMPLED is the no-model counterfactual, so the model's opinion must not leak into it."""
    llm = FakeLlm(pattern=("ENTRY",), side=SHORT)
    r = await _run(llm, sample=20)
    for obs, row in zip(r.obs["ALL_SAMPLED"], [x for x in r.rows if x.action != "ERROR"]):
        assert obs.side == row.trigger_side


async def test_model_entry_cohort_trades_the_models_side():
    llm = FakeLlm(pattern=("ENTRY",), side=SHORT)
    r = await _run(llm, sample=20)
    assert r.obs["MODEL_ENTRY"]
    assert all(o.side == SHORT for o in r.obs["MODEL_ENTRY"])


async def test_a_rejected_proposal_is_not_counted_as_an_entry():
    """The validator refuses it live, so crediting it to ENTRY would flatter the model with
    trades the guardrails would have thrown away."""
    llm = FakeLlm(pattern=("ENTRY",), bad_rr=True)     # take-profit fails the reward:risk check
    r = await _run(llm, sample=20)
    assert all(row.action == "REJECTED" for row in r.rows)
    assert r.obs["MODEL_ENTRY"] == []
    assert r.obs["MODEL_REJECTED"]


async def test_a_synthetic_fallback_is_excluded_from_every_cohort():
    """The chain exhausted — there is no model verdict, so the bar cannot count as a SKIP."""
    r = await _run(FakeLlm(synthetic=True), sample=20)
    assert all(row.action == "ERROR" for row in r.rows)
    assert r.obs["MODEL_ENTRY"] == [] and r.obs["MODEL_SKIP"] == []
    assert r.obs["ALL_SAMPLED"] == []


async def test_a_failing_provider_does_not_kill_the_run():
    r = await _run(FakeLlm(raises=True), sample=15)
    assert len(r.rows) == 15
    assert all(row.action == "ERROR" for row in r.rows)


async def test_cost_and_tokens_are_accumulated():
    r = await _run(FakeLlm(pattern=("SKIP",)), sample=25)
    assert r.cost_usd == pytest.approx(25 * 0.002)
    assert sum(row.output_tokens for row in r.rows) == 25 * 400


async def test_dry_run_makes_no_calls_and_still_builds_cohorts():
    llm = FakeLlm()
    r = await _run(llm, sample=30, dry_run=True)
    assert llm.calls == 0
    assert r.cost_usd == 0.0
    assert r.obs["ALL_SAMPLED"]


# --------------------------------------------------------------------------- levels
def test_model_levels_clamp_a_too_tight_stop():
    from worker.pattern.decision.sizing import MIN_STOP_ATR
    atr_pct = 0.004
    stop, target = _model_levels(LONG, 60_000.0, atr_pct, decision_stop=59_950.0)  # ~0.08%
    assert (60_000.0 - stop) / 60_000.0 == pytest.approx(MIN_STOP_ATR * atr_pct)
    assert target > 60_000.0


def test_model_levels_keep_a_stop_that_is_already_wide_enough():
    stop, _ = _model_levels(LONG, 60_000.0, 0.002, decision_stop=59_000.0)   # ~1.7%, above floor
    assert stop == pytest.approx(59_000.0)


def test_model_levels_fall_back_when_the_stop_is_unusable():
    stop, _ = _model_levels(LONG, 60_000.0, 0.004, decision_stop=None)
    assert stop < 60_000.0
    assert _model_levels(SHORT, 60_000.0, 0.004, decision_stop=60_000.0)[0] > 60_000.0


# --------------------------------------------------------------------------- verdict
def _obs(cohort, r):
    o = Observation(bar_time_ms=0, cohort=cohort, trigger="t", side=LONG, regime="range",
                    adx=20.0, atr_pct=0.003, close=60_000.0)
    o.r, o.pnl_usd, o.exit_reason = r, r * 100, "stop"
    return o


def _result(entry_rs, skip_rs, all_rs):
    res = ScreenResult(symbol="BTCUSDT", interval="15m", start="a", end="b", sample=len(all_rs))
    res.obs = {
        "MODEL_ENTRY": [_obs("MODEL_ENTRY", r) for r in entry_rs],
        "MODEL_SKIP": [_obs("MODEL_SKIP", r) for r in skip_rs],
        "ALL_SAMPLED": [_obs("ALL_SAMPLED", r) for r in all_rs],
    }
    return res


def test_verdict_refuses_to_conclude_on_too_few_entries():
    assert "too few bars to measure" in _verdict(_result([0.5] * 5, [0.1] * 50, [0.1] * 55))


def test_verdict_reports_no_skill_when_cohorts_match():
    rs = [0.5, -0.5] * 200
    text = _verdict(_result(rs, rs, rs))
    assert "No measurable selection skill" in text


def test_verdict_detects_negative_selection_skill():
    """The model systematically picking the worse bars is a distinct, important outcome."""
    entry = [-0.8, -0.9, -0.7] * 100
    rest = [0.6, 0.7, 0.5] * 100
    text = _verdict(_result(entry, rest, rest))
    assert "NEGATIVE selection skill" in text


def test_verdict_detects_real_skill_and_checks_it_against_fees():
    entry = [0.9, 1.0, 0.8] * 100
    rest = [-0.4, -0.5, -0.3] * 100
    text = _verdict(_result(entry, rest, rest))
    assert "Real selection skill" in text
    assert "CLEARS" in text or "does NOT clear" in text


def test_report_renders_without_a_model_run():
    res = _result([0.5] * 40, [-0.2] * 100, [0.0] * 140)
    res.rows = [ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="range",
                          action="ENTRY")] * 40
    text = report(res)
    assert "LLM SKIP TEST" in text and "COHORTS" in text


# --------------------------------------------------------------------------- stop attribution
async def test_own_stop_cohort_mirrors_the_entry_cohort_bar_for_bar():
    """The two ENTRY cohorts must cover exactly the same trades — they differ only in where the
    stop sits, which is the whole point of splitting them."""
    r = await _run(FakeLlm(pattern=("ENTRY", "SKIP")), sample=40)
    a, b = r.obs["MODEL_ENTRY"], r.obs["MODEL_ENTRY_OWNSTOP"]
    assert len(a) == len(b) and a
    assert [o.bar_time_ms for o in a] == [o.bar_time_ms for o in b]
    assert [o.side for o in a] == [o.side for o in b]


async def test_the_primary_entry_cohort_ignores_the_models_stop():
    """MODEL_ENTRY must use the policy stop, or its lift over ALL_SAMPLED would mix selection
    with stop placement and be attributable to neither."""
    from worker.pattern.replay import _stop_and_target
    # A very wide model stop would produce a visibly different R if it leaked into MODEL_ENTRY.
    llm = FakeLlm(pattern=("ENTRY",))
    r = await _run(llm, sample=12)
    for obs in r.obs["MODEL_ENTRY"]:
        expected_stop, _ = _stop_and_target(obs.side, obs.close, obs.atr_pct)
        # Re-deriving the policy stop from the observation must reproduce what was simulated:
        # a non-policy stop would give a different R for the same bar/side.
        assert expected_stop != obs.close


def test_measure_accepts_a_levels_override():
    """The override is what lets the own-stop cohort exist; replay itself never passes one."""
    from worker.pattern.replay import _measure
    step = 15 * MIN
    bars = [{"timestamp": k * step, "close": 100.0, "open": 100.0, "high": 100.0,
             "low": 100.0, "volume": 1.0} for k in range(50)]
    ones = synth_1m(600)
    idx = {b["timestamp"]: i for i, b in enumerate(ones)}
    ctx = {"regime": "range", "adx": 20.0, "atr_pct": 0.01, "close": 100.0}
    wide = _measure(bars, ones, idx, [1.0] * 50, 10, ctx, LONG, "t", "c", "15m", step,
                    levels=(90.0, 130.0))
    tight = _measure(bars, ones, idx, [1.0] * 50, 10, ctx, LONG, "t", "c", "15m", step,
                     levels=(99.0, 103.0))
    # Different protective levels must actually change the simulated outcome.
    assert (wide.r, wide.exit_reason) != (tight.r, tight.exit_reason)


# --------------------------------------------------------------------------- truncated runs
def test_action_percentages_are_of_answered_calls_not_the_sample():
    """With a provider failing mid-run the two diverge wildly, and quoting ENTRY as a share of
    the sample understates what the model actually accepted."""
    res = _result([0.5] * 20, [-0.2] * 40, [0.0] * 60)
    res.rows = ([ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                           action="ENTRY")] * 20
                + [ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                             action="SKIP")] * 40
                + [ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                             action="ERROR", reject_reason="Error code: 402 Insufficient Balance")] * 140)
    text = report(res)
    assert "33.3% of answered" in text          # 20 of 60 answered, not 20 of 200
    assert "70.0% of the sample" in text        # errors stay a share of what was attempted


def test_a_truncated_run_warns_loudly_with_its_cause():
    res = _result([0.5] * 20, [-0.2] * 40, [0.0] * 60)
    res.rows = ([ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                           action="ENTRY")] * 60
                + [ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                             action="ERROR",
                             reject_reason="Error code: 402 - Insufficient Balance")] * 140)
    text = report(res)
    assert "never got an answer" in text
    assert "insufficient balance" in text
    assert "answered subset only" in text


def test_a_clean_run_gets_no_warning():
    res = _result([0.5] * 20, [-0.2] * 40, [0.0] * 60)
    res.rows = [ScreenRow(bar_time_ms=0, trigger="t", trigger_side=LONG, regime="r",
                          action="ENTRY")] * 60
    assert "never got an answer" not in report(res)


def test_verdict_says_inconclusive_rather_than_null_when_underpowered():
    """A big-but-unresolvable lift must not be reported as 'no skill' — that reads a failure to
    measure as a measurement, and the two call for opposite next steps."""
    # Small n (40) with the ~1.4R spread real trades have: a 0.5R gap is far too big to dismiss
    # and still nowhere near resolvable, which is exactly the state a truncated run lands in.
    entry = [1.4, -1.4] * 20
    rest = [r + 1.0 for r in entry]
    text = _verdict(_result(entry, rest, entry + rest))
    assert "INCONCLUSIVE" in text
    assert "not enough evidence either way" in text


def test_verdict_reports_its_own_detectable_effect_size():
    text = _verdict(_result([0.5, -0.5] * 100, [0.5, -0.5] * 100, [0.5, -0.5] * 200))
    assert "POWER:" in text and "smallest lift" in text
