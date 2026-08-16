"""Does the model's SKIP earn its keep?

The replay harness measures the deterministic layer with the LLM removed, and over a year of
BTCUSDT it came back at roughly break-even before costs: a small directional edge, entirely eaten
by fees. But that is not the system. Live, the model is asked about every gate-approved bar and
REFUSES most of them, and if those refusals are better than random then live selection beats what
the replay measured.

This module tests exactly that, and only that:

    take a random sample of gate-approved bars
      -> ask the REAL model the REAL entry prompt
      -> run the ladder on the bars it chose, its way
      -> compare against the same bars taken the trigger's way, and against the ones it refused

The cohorts:

    MODEL_ENTRY          bars the model accepted, traded the side IT picked, with the SAME policy
                         stop every other cohort uses
    MODEL_ENTRY_OWNSTOP  the same bars and sides, but with the model's own (policy-clamped) stop
    MODEL_SKIP           bars the model refused, traded the TRIGGER's side — the counterfactual,
                         i.e. what would have happened had we overruled every refusal
    MODEL_REJECTED       proposals the validator threw out; never counted as trades
    ALL_SAMPLED          every sampled bar, traded the trigger's side — the no-model baseline

The question is whether MODEL_ENTRY beats ALL_SAMPLED by more than trading costs. Selection that
adds less than the fee it triggers is not selection worth paying for.

Stop placement is held FIXED between MODEL_ENTRY and ALL_SAMPLED on purpose. The model influences
a trade two ways — which bars it takes and where it puts the stop — and folding both into one
number would leave a lift unattributable to either. MODEL_ENTRY_OWNSTOP exists to price the second
effect separately: the gap between it and MODEL_ENTRY is what the model's stop placement is worth.

WHY THIS COSTS MONEY AND THE REPLAY DOES NOT
This makes one real API call per sampled bar. At DeepSeek v4-flash's measured ~1.3k in / 4.4k out
that is fractions of a cent each, but it is real spend and it scales linearly with --sample.

STATISTICAL POWER
Per-trade R has sd ~= 1.43 on this instrument. Comparing an ENTRY cohort of ~22% of the sample
against the rest, the detectable difference at |t| = 2 is roughly 6.9 / sqrt(sample). Fees cost
about 0.16R per trade, so a sample of ~2000 (detecting ~0.15R) is the point where the test can
actually answer "does selection cover its own costs". Below ~800 the test can only find effects
far larger than anything plausible, and a null result would mean nothing.

USAGE
    python -m worker.pattern.llm_screen --start 2025-08-01 --end 2026-08-01 --sample 2000
    python -m worker.pattern.llm_screen --start 2026-01-01 --sample 50 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import statistics as stats
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import config
from .decision.engine import ENTRY_MAX_TOKENS, _is_synthetic, build_account_snapshot
from .decision.prompts import MIN_RISK_REWARD, build_entry_prompt
from .decision.schemas import EntryDecision
from .decision.sizing import clamp_stop_pct, take_profit_for
from .decision.validator import validate_entry
from .features import build_feature_state
from .replay import (
    LONG, NOTIONAL_USD, Observation, _agg, _fmt, _measure, _stop_and_target,
    _welch_t, bucket_ms, load_1m, rollup, walk_gate,
)

log = logging.getLogger("worker.pattern.llm_screen")

# The balance the prompt is told about. Fixed so every sampled bar sees an identical account —
# a varying budget would make the model's answer depend on replay bookkeeping rather than on the
# market, and this test is about the market read.
SCREEN_BALANCE_USD = 10_000.0

# USD per million tokens, used ONLY to show what a run cost. LlmService.PRICING deliberately
# carries no DeepSeek entry — it logs $0 rather than a fabricated estimate, which is the right
# call for per-decision accounting but leaves a spend-tracking tool reporting $0.00 on a run that
# really cost money. These are DeepSeek's published cache-miss rates; override them at the CLI if
# they have moved, and treat the figure as an ESTIMATE, not an invoice.
DEEPSEEK_PRICE_IN, DEEPSEEK_PRICE_OUT = 0.28, 0.42


@dataclass
class ScreenRow:
    bar_time_ms: int
    trigger: str
    trigger_side: str
    regime: str
    action: str                      # 'ENTRY' | 'SKIP' | 'REJECTED' | 'ERROR'
    model_side: Optional[str] = None
    model_stop: Optional[float] = None
    confidence: Optional[float] = None
    reasoning: str = ""
    reject_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class ScreenResult:
    symbol: str
    interval: str
    start: str
    end: str
    sample: int
    approved_total: int = 0
    rows: list[ScreenRow] = field(default_factory=list)
    # Cohort observations, keyed by cohort name.
    obs: dict = field(default_factory=dict)
    elapsed_s: float = 0.0

    price_in: float = DEEPSEEK_PRICE_IN
    price_out: float = DEEPSEEK_PRICE_OUT

    @property
    def cost_usd(self) -> float:
        """What the provider reported, when it reports anything."""
        return sum(r.cost_usd for r in self.rows)

    @property
    def estimated_cost_usd(self) -> float:
        """Token-based estimate — the only figure available for a model absent from PRICING."""
        tin = sum(r.input_tokens for r in self.rows)
        tout = sum(r.output_tokens for r in self.rows)
        return tin * self.price_in / 1e6 + tout * self.price_out / 1e6


# --------------------------------------------------------------------------- one bar


async def _ask(llm, sem, bars, symbol, interval, candle_limit, i, ctx, trig, account, dry_run):
    """Build the real prompt for bar `i` and get the model's verdict."""
    state = build_feature_state(symbol, symbol, interval, bars[i - candle_limit:i + 1])
    row = ScreenRow(bar_time_ms=bars[i]["timestamp"], trigger=trig["name"],
                    trigger_side=trig["side"], regime=ctx["regime"], action="SKIP")
    if dry_run:
        # Exercises everything except the spend: prompt construction, sampling, cohorts.
        row.action, row.model_side = ("ENTRY", trig["side"]) if i % 4 == 0 else ("SKIP", None)
        return row, state

    prompt = build_entry_prompt(state.to_state_pack(), account, [trig], [])
    async with sem:
        try:
            response = await llm.generate_structured_completion(
                prompt, EntryDecision, max_tokens=ENTRY_MAX_TOKENS)
        except Exception as err:  # noqa: BLE001 — one bad call must not end the run
            row.action, row.reject_reason = "ERROR", str(err)[:200]
            return row, state

    row.input_tokens = int(response.get("inputTokens") or 0)
    row.output_tokens = int(response.get("outputTokens") or 0)
    row.cost_usd = float(response.get("costUsd") or 0.0)
    if _is_synthetic(response):
        # The chain exhausted. Live this is refused as a trade; here it must be excluded from the
        # cohorts entirely rather than counted as a SKIP the model meant.
        row.action, row.reject_reason = "ERROR", "synthetic fallback"
        return row, state

    decision: EntryDecision = response["data"]
    row.reasoning = (decision.reasoning or "")[:300]
    row.confidence = decision.confidence
    if decision.action == "SKIP":
        return row, state

    # The live path validates before it sizes, and a rejected proposal is NOT a trade — counting
    # it as one would credit the model for entries the guardrails would have thrown away.
    verdict = validate_entry(decision, current_price=state.close,
                             regime_label=state.regime["label"], min_rr=MIN_RISK_REWARD)
    if not verdict.ok:
        row.action, row.reject_reason, row.model_side = "REJECTED", verdict.reason, decision.side
        return row, state

    row.action, row.model_side = "ENTRY", decision.side
    row.model_stop = float(decision.stop_loss) if decision.stop_loss else None
    return row, state


def _model_levels(side: str, close: float, atr_pct: float, decision_stop: Optional[float]):
    """The stop/target a live entry would really get: the model's stop, clamped by policy.

    Falls back to the policy floor when the model's number is unusable, which is what sizing
    would end up applying anyway.
    """
    if decision_stop is None or decision_stop <= 0:
        return _stop_and_target(side, close, atr_pct)
    requested_pct = abs(close - decision_stop) / close
    if requested_pct <= 0:
        return _stop_and_target(side, close, atr_pct)
    stop_pct, _ = clamp_stop_pct(requested_pct, atr_pct)
    stop = close * (1 - stop_pct) if side == LONG else close * (1 + stop_pct)
    return stop, take_profit_for(side, close, stop, MIN_RISK_REWARD)


# --------------------------------------------------------------------------- run


async def screen(
    bars_1m: list[dict], *, symbol: str, interval: str, candle_limit: int, sample: int,
    seed: int = 11, concurrency: int = 12, dry_run: bool = False,
) -> ScreenResult:
    interval = interval or config.pattern_interval
    bars = rollup(bars_1m, interval)
    step = bucket_ms(interval)
    index_1m = {b["timestamp"]: i for i, b in enumerate(bars_1m)}

    log.info("Walking the gate over %s %s bars...", f"{len(bars):,}", interval)
    approved, atr_by_bar, _skips, _evaluated = walk_gate(
        bars, symbol=symbol, interval=interval, candle_limit=candle_limit, mode="signals")

    result = ScreenResult(symbol=symbol, interval=interval, sample=sample,
                          approved_total=len(approved),
                          start=_iso(bars[0]["timestamp"]) if bars else "",
                          end=_iso(bars[-1]["timestamp"]) if bars else "")
    if not approved:
        return result

    rng = random.Random(seed)
    picked = rng.sample(approved, min(sample, len(approved)))
    log.info("%s gate-approved bars; sampling %s%s.",
             f"{len(approved):,}", f"{len(picked):,}", " (DRY RUN — no API calls)" if dry_run else "")

    account = build_account_snapshot(day_start_balance=SCREEN_BALANCE_USD, realized_loss_today=0.0,
                                     free_balance=SCREEN_BALANCE_USD, filters=None)
    llm = None
    if not dry_run:
        from ..llm.service import LlmService
        llm = LlmService()

    sem = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    done = 0

    async def one(item):
        nonlocal done
        i, ctx, trig = item
        row, _state = await _ask(llm, sem, bars, symbol, interval, candle_limit, i, ctx, trig,
                                 account, dry_run)
        done += 1
        if done % 100 == 0:
            rate = done / max(time.perf_counter() - started, 1e-9)
            log.info("  ...%d/%d answered (%.1f/s, $%.2f so far)",
                     done, len(picked), rate, sum(r.cost_usd for r in result.rows))
        return item, row

    answers = await asyncio.gather(*(one(item) for item in picked))
    result.elapsed_s = time.perf_counter() - started

    # --- Cohorts. Every sampled bar contributes to ALL_SAMPLED; its model verdict decides which
    # of the other two it joins.
    cohorts: dict[str, list[Observation]] = {"MODEL_ENTRY": [], "MODEL_ENTRY_OWNSTOP": [],
                                             "MODEL_SKIP": [], "MODEL_REJECTED": [],
                                             "ALL_SAMPLED": []}
    for (i, ctx, trig), row in answers:
        result.rows.append(row)
        if row.action == "ERROR":
            continue   # no verdict -> belongs in no cohort, not even the baseline

        baseline = _measure(bars, bars_1m, index_1m, atr_by_bar, i, ctx, trig["side"],
                            trig["name"], "ALL_SAMPLED", interval, step)
        cohorts["ALL_SAMPLED"].append(baseline)

        if row.action == "ENTRY":
            side = row.model_side or trig["side"]
            # Primary cohort: the model's SIDE, but the same policy stop every other cohort uses.
            # Holding stop placement fixed is what makes the lift attributable to selection.
            cohorts["MODEL_ENTRY"].append(
                _measure(bars, bars_1m, index_1m, atr_by_bar, i, ctx, side, trig["name"],
                         "MODEL_ENTRY", interval, step))
            # Secondary cohort: the model's side AND its own (policy-clamped) stop. The gap
            # between the two isolates whether its stop placement is worth anything on its own —
            # a separate question from whether its refusals are, and one that would be invisible
            # if both effects were folded into a single number.
            cohorts["MODEL_ENTRY_OWNSTOP"].append(
                _measure(bars, bars_1m, index_1m, atr_by_bar, i, ctx, side, trig["name"],
                         "MODEL_ENTRY_OWNSTOP", interval, step,
                         levels=_model_levels(side, ctx["close"], ctx["atr_pct"], row.model_stop)))
        elif row.action == "REJECTED":
            cohorts["MODEL_REJECTED"].append(
                _measure(bars, bars_1m, index_1m, atr_by_bar, i, ctx, row.model_side or trig["side"],
                         trig["name"], "MODEL_REJECTED", interval, step))
        else:
            # What we would have made by ignoring the refusal and taking the trigger anyway.
            cohorts["MODEL_SKIP"].append(
                _measure(bars, bars_1m, index_1m, atr_by_bar, i, ctx, trig["side"], trig["name"],
                         "MODEL_SKIP", interval, step))

    result.obs = cohorts
    return result


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- report


def report(result: ScreenResult) -> str:
    rows = result.rows
    n = len(rows)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.action] = counts.get(r.action, 0) + 1
    answered = n - counts.get("ERROR", 0)

    lines = [
        "",
        f"LLM SKIP TEST  {result.symbol} {result.interval}  {result.start} -> {result.end}",
        "=" * 104,
        f"gate-approved bars in window: {result.approved_total:,}   sampled: {n:,}   "
        f"answered: {answered:,}",
        f"elapsed: {result.elapsed_s / 60:.1f} min   "
        f"tokens: {sum(r.input_tokens for r in rows):,} in / {sum(r.output_tokens for r in rows):,} out",
        f"spend: ${result.cost_usd:.2f} reported by the provider; "
        f"~${result.estimated_cost_usd:.2f} estimated at "
        f"${result.price_in:.2f}/${result.price_out:.2f} per M tokens",
        "",
        "What the model did with the bars the gate paid for:",
    ]
    for action in ("ENTRY", "SKIP", "REJECTED"):
        c = counts.get(action, 0)
        if c:
            # Percentages are of ANSWERED calls, not of the sample. With a failing provider the
            # two diverge wildly, and "ENTRY 10%" when the model actually accepted 31% of the
            # bars it saw is the kind of number that gets quoted later and is simply wrong.
            lines.append(f"  {c:>7,}  {action:<9} {c / max(answered, 1) * 100:>5.1f}% of answered")
    if counts.get("ERROR"):
        lines.append(f"  {counts['ERROR']:>7,}  {'ERROR':<9} "
                     f"{counts['ERROR'] / max(n, 1) * 100:>5.1f}% of the sample — no verdict")
        lines += _error_warning(counts["ERROR"], n, rows)

    hdr = (f"\n{'cohort':<18}{'n':>7}{'exp R':>9}{'t':>7}{'win%':>7}{'tgt%':>7}{'PF':>7}"
           f"{'net $':>11}{'fees $':>10}")

    def row_of(label: str, obs: list[Observation]) -> str:
        a = _agg(obs)
        return (f"{label:<18}{a['n']:>7,}{_fmt(a['expectancy_r'], 3, 9)}{_fmt(a['t_stat'], 1, 7)}"
                f"{_fmt(a['win_rate'], 1, 7)}{_fmt(a['target_rate'], 1, 7)}"
                f"{_fmt(a['profit_factor'], 2, 7)}{a['total_pnl']:>11,.0f}{a['fees']:>10,.0f}")

    lines += ["", "COHORTS", hdr, "-" * len(hdr.strip())]
    for name in ("MODEL_ENTRY", "MODEL_ENTRY_OWNSTOP", "MODEL_SKIP", "MODEL_REJECTED",
                 "ALL_SAMPLED"):
        obs = result.obs.get(name) or []
        if obs:
            lines.append(row_of(name, obs))

    lines += ["", "VERDICT", "-" * 70, _verdict(result)]
    return "\n".join(lines)


def _error_warning(errors: int, sampled: int, rows: list[ScreenRow]) -> list[str]:
    """Say loudly when the run did not actually happen.

    A truncated run still prints a full-looking table, and a reader who skims to the VERDICT has
    no way to tell it came from a third of the sample. The most common cause is the provider
    cutting the key off mid-run (402 / rate limit), which is a billing problem wearing the costume
    of a scientific result.
    """
    share = errors / max(sampled, 1)
    if share < 0.05:
        return []
    causes: dict[str, int] = {}
    for r in rows:
        if r.action == "ERROR" and r.reject_reason:
            key = ("insufficient balance" if "Insufficient Balance" in r.reject_reason
                   else "rate limited" if "429" in r.reject_reason
                   else "chain exhausted" if "synthetic" in r.reject_reason
                   else r.reject_reason[:60])
            causes[key] = causes.get(key, 0) + 1
    out = ["", f"  !! {share * 100:.0f}% of the sample never got an answer. Top causes:"]
    for cause, c in sorted(causes.items(), key=lambda kv: -kv[1])[:3]:
        out.append(f"       {c:>6,}  {cause}")
    out.append("     Every figure below is from the answered subset only.")
    return out


def _verdict(result: ScreenResult) -> str:
    entry = _agg(result.obs.get("MODEL_ENTRY") or [])
    skip = _agg(result.obs.get("MODEL_SKIP") or [])
    allb = _agg(result.obs.get("ALL_SAMPLED") or [])

    if entry["n_resolved"] < 30:
        return (f"Only {entry['n_resolved']} resolved ENTRY trades — the model accepted too few "
                "bars to measure. Raise --sample.")

    out = []
    lift = entry["expectancy_r"] - allb["expectancy_r"]
    t_lift = _welch_t(entry["r_values"], allb["r_values"])
    out.append(f"SELECTION LIFT: model-chosen {entry['expectancy_r']:+.4f}R vs "
               f"taking every sampled bar {allb['expectancy_r']:+.4f}R "
               f"-> lift {lift:+.4f}R (t = {t_lift:+.1f}).")

    # What this run could actually have detected. Without it a null reads as "no effect" when it
    # often means "not enough data to see one" — and those call for opposite next steps.
    n_e, n_rest = entry["n_resolved"], max(allb["n_resolved"] - entry["n_resolved"], 1)
    sd = stats.stdev(entry["r_values"]) if len(entry["r_values"]) > 1 else 1.4
    mde = 2 * sd * math.sqrt(1 / max(n_e, 1) + 1 / n_rest)
    out.append(f"POWER: with {n_e} accepted and {n_rest} not, the smallest lift this run could "
               f"have resolved is about {mde:.3f}R.")

    if skip["n_resolved"] >= 30:
        t_es = _welch_t(entry["r_values"], skip["r_values"])
        out.append(f"REFUSALS: bars it declined were worth {skip['expectancy_r']:+.4f}R had we "
                   f"overruled it; accepted vs declined t = {t_es:+.1f}.")

    # Fees per trade in R, which is the bar the lift has to clear to be worth paying for.
    fee_r = (allb["fees"] / max(allb["n_resolved"], 1)) / (NOTIONAL_USD * _typical_stop_pct(result))
    out.append("")
    if math.isnan(t_lift) or abs(t_lift) < 2:
        if abs(lift) > mde * 0.5:
            # A lift of interesting SIZE that the sample is simply too small to confirm. Calling
            # that "no skill" would be reading a failure to measure as a measurement.
            out.append(f"INCONCLUSIVE. The observed lift ({lift:+.3f}R) is large enough to matter "
                       f"but the sample cannot resolve it (needs ~{mde:.3f}R). This is not "
                       "evidence of no effect — it is not enough evidence either way. Re-run with "
                       "a larger --sample before drawing any conclusion.")
        else:
            out.append("No measurable selection skill. The model's accept/refuse split does not "
                       "separate good bars from bad ones, and the effect is small enough that "
                       "this sample would have seen it. On this evidence the LLM adds cost and "
                       "latency without adding edge, and the live system is effectively the "
                       "deterministic layer the replay already measured.")
    elif lift < 0:
        out.append("NEGATIVE selection skill: the model is systematically choosing the WORSE "
                   "bars. Its refusals are better trades than its acceptances.")
    else:
        out.append(f"Real selection skill: +{lift:.4f}R per trade over taking everything.")
        out.append(f"Fees cost about {fee_r:.3f}R per trade. The lift "
                   + ("CLEARS" if lift > fee_r else "does NOT clear")
                   + " that, so on this evidence the model's screening "
                   + ("pays for the trading it causes." if lift > fee_r
                      else "is real but still too small to trade profitably."))
    return "\n".join(out)


def _typical_stop_pct(result: ScreenResult) -> float:
    """Median stop distance across the sampled bars — used only to express fees in R."""
    obs = result.obs.get("ALL_SAMPLED") or []
    pcts = [o.atr_pct for o in obs if o.atr_pct > 0]
    from .decision.sizing import MIN_STOP_ATR
    return (stats.median(pcts) * MIN_STOP_ATR) if pcts else 0.005


# --------------------------------------------------------------------------- CLI


async def _main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end
           else datetime.now(timezone.utc))
    bars_1m = await load_1m(args.symbol, start, end)
    if not bars_1m:
        log.error("No stored bars in that range.")
        return 1
    log.info("Loaded %s 1m bars.", f"{len(bars_1m):,}")

    result = await screen(bars_1m, symbol=args.symbol, interval=args.interval,
                          candle_limit=args.candle_limit, sample=args.sample,
                          concurrency=args.concurrency, dry_run=args.dry_run)
    result.price_in, result.price_out = args.price_in, args.price_out
    print(report(result))
    if args.json:
        from dataclasses import asdict
        with open(args.json, "w") as fh:
            json.dump({"symbol": result.symbol, "start": result.start, "end": result.end,
                       "approvedTotal": result.approved_total, "costUsd": result.cost_usd,
                       "estimatedCostUsd": result.estimated_cost_usd,
                       "rows": [asdict(r) for r in result.rows],
                       "cohorts": {k: [asdict(o) for o in v] for k, v in result.obs.items()}},
                      fh, indent=2)
        log.info("Wrote %s", args.json)
    return 0


def run() -> None:
    p = argparse.ArgumentParser(description="Test whether the model's SKIP adds edge. COSTS MONEY.")
    p.add_argument("--symbol", default=config.backfill_symbol)
    p.add_argument("--interval", default=config.pattern_interval)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None)
    p.add_argument("--candle-limit", type=int, default=config.pattern_candle_limit)
    p.add_argument("--sample", type=int, default=2000, help="LLM calls to make (one per bar)")
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--dry-run", action="store_true", help="no API calls; exercises the pipeline")
    p.add_argument("--price-in", type=float, default=DEEPSEEK_PRICE_IN,
                   help="USD per M input tokens, for the cost estimate only")
    p.add_argument("--price-out", type=float, default=DEEPSEEK_PRICE_OUT,
                   help="USD per M output tokens, for the cost estimate only")
    p.add_argument("--json", default=None)
    raise SystemExit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    run()
