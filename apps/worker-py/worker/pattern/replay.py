"""Offline replay harness — does the deterministic layer have any edge?

The first paper week produced 41 trades and a loss, and 41 trades cannot tell you WHY. Measured
over those trades the entry signal had a negative forward return at every horizon, but at n=41
that reading is barely distinguishable from noise. This harness answers the same question over
years instead of a week, and it does it without spending a cent on the LLM.

WHAT IT MEASURES, AND WHAT IT DOES NOT
--------------------------------------
The live loop is: features -> gate -> LLM -> validate -> size -> order. Everything before the LLM
is deterministic, and this harness replays exactly that part, using the SAME `build_feature_state`
and the SAME `evaluate_gate` the live loop calls. No reimplementation — a harness that models the
engine instead of running it proves nothing about the engine.

Where the gate approves a bar, the LLM would then pick a side. Here the TRIGGER's own side is
taken instead. That is the useful substitution: it isolates the question "is the deterministic
layer handing the model anything with an edge in it?" If a trigger's own direction has no forward
edge, then the model is being asked to find signal in noise, and no amount of prompt work fixes
that. If it does have an edge, the model's job is to select among them and the prompt is worth
tuning.

So: a positive result here is necessary but not sufficient for a profitable system. A negative
result is close to decisive — it says the raw material is bad.

HONEST LIMITATIONS
------------------
  * The ~45s the LLM takes to answer is not modelled; entries fill at the decision bar's close.
    Live, price has moved by then. This flatters the result slightly.
  * The model's SKIP is not modelled. Live, the model refuses ~85% of gate-approved bars, and if
    its refusals are better than random the live selection beats what is measured here.
  * Stop distance uses the policy floor (MIN_STOP_ATR), not a level the model chose. That is what
    a clamped model stop becomes anyway, but a well-placed discretionary stop could do better.
  * Exits are driven on 1m closes, matching the live tick loop, so intrabar wicks are missed in
    exactly the way they are missed live. This is faithful, not optimistic.

Costs (taker fees both legs + simulated slippage) come from the same functions the execution
engine charges with, so an R-multiple here is comparable to a real one.

USAGE
-----
    python -m worker.pattern.replay --start 2025-01-01 --end 2026-01-01
    python -m worker.pattern.replay --start 2024-01-01 --mode sequential --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import math
import random
import re
import statistics as stats
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..config import config
from ..execution.execution import slipped_fill, taker_fee_pct
from .decision.gate import COOLDOWN_BARS, ENTRY, evaluate_gate
from .decision.prompts import MIN_RISK_REWARD
from .decision.sizing import MIN_STOP_ATR, clamp_stop_pct
from .exits import MAX_HOLD_BARS, RUNNER, better_extreme, evaluate_exit, r_multiple
from .features import build_feature_state

log = logging.getLogger("worker.pattern.replay")

LONG = "long"
SHORT = "short"
MINUTE_MS = 60_000

# Forward-return horizons, in bars of the replay interval. On 15m these are 15m/1h/4h/8h/24h.
FORWARD_HORIZONS = (1, 4, 16, 32, 96)

# Notional every simulated trade is measured at. Fixed rather than risk-sized on purpose: the
# question here is "does the signal have an edge", and letting size vary would mix a sizing
# question into the answer. R-multiples are size-independent anyway; the dollar column exists
# only to make the fee drag legible.
NOTIONAL_USD = 5_000.0


# --------------------------------------------------------------------------- data


def bucket_ms(interval: str) -> int:
    """Interval string -> milliseconds. Mirrors store.interval_ms, kept local so the harness can
    run against a plain candle list with no DB import."""
    unit, n = interval[-1].lower(), int(interval[:-1])
    return n * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[unit] * 1000


_FLOW_FIELDS = ("quote_volume", "trades", "taker_buy_base", "taker_buy_quote")


def rollup(bars_1m: list[dict], interval: str) -> list[dict]:
    """Aggregate 1m bars into `interval` bars.

    Buckets are floored to the epoch, which is exactly what TimescaleDB's `time_bucket` does, so
    a replayed bar has the same boundaries as the live one. A partial trailing bucket is dropped:
    the live engine never evaluates an incomplete bar (see executor._drop_incomplete_bar) and a
    replay that did would be reading the future through a half-formed candle.
    """
    step = bucket_ms(interval)
    out: list[dict] = []
    cur: Optional[dict] = None
    cur_key = -1
    for b in bars_1m:
        key = (b["timestamp"] // step) * step
        if key != cur_key:
            if cur is not None:
                out.append(cur)
            cur = {"timestamp": key, "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"], "volume": b["volume"]}
            for fld in _FLOW_FIELDS:
                cur[fld] = b.get(fld)
            cur_key = key
        else:
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] += b["volume"]
            # Additive, so they sum like volume. Summing the components and dividing at the end
            # is not the same as averaging per-minute ratios — the latter would weight a thin
            # minute the same as a heavy one. A None anywhere in the bucket poisons the whole
            # bar rather than being treated as zero.
            for fld in _FLOW_FIELDS:
                a, b_val = cur.get(fld), b.get(fld)
                cur[fld] = None if (a is None or b_val is None) else a + b_val
    if cur is not None:
        # Keep the last bucket only if the 1m data actually covers it to the end.
        if bars_1m[-1]["timestamp"] >= cur_key + step - MINUTE_MS:
            out.append(cur)
    return out


def _opt(v):
    """Numeric or None — never a substituted zero. See the note in the flow-columns migration."""
    return None if v is None else float(v)


async def load_1m(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Every stored 1m bar in [start, end), as plain dicts with int-ms timestamps."""
    from ..db import get_pool

    pool = await get_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        # STREAMED through a server-side cursor, not conn.fetch(). fetch() buffers every Record
        # in memory and the comprehension over it then builds a second full copy — at six years
        # of 1m bars (~3.2M rows) that peak is a couple of gigabytes and the process is OOM-killed
        # mid-load, which looks exactly like a silent crash. A cursor holds one chunk at a time,
        # so peak memory is the output list alone.
        async with conn.transaction():
            async for r in conn.cursor(
                """
                SELECT o.timestamp, o.open, o.high, o.low, o.close, o.volume,
                       o.quote_volume, o.trades, o.taker_buy_base, o.taker_buy_quote
                FROM ohlcv_data o JOIN tickers t ON t.id = o.ticker_id
                WHERE t.symbol = $1 AND o.timestamp >= $2 AND o.timestamp < $3
                ORDER BY o.timestamp ASC
                """,
                symbol, start, end, prefetch=50_000,
            ):
                out.append({
                    "timestamp": int(r["timestamp"].timestamp() * 1000), "open": float(r["open"]),
                    "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r["volume"]),
                    # None, not 0.0, when the row predates the flow columns. A zero
                    # taker_buy_base would read as 100% aggressive selling — a strong signal,
                    # entirely fabricated.
                    "quote_volume": _opt(r["quote_volume"]),
                    "trades": _opt(r["trades"]),
                    "taker_buy_base": _opt(r["taker_buy_base"]),
                    "taker_buy_quote": _opt(r["taker_buy_quote"]),
                })
    return out


# --------------------------------------------------------------------------- ladder


@dataclass
class LadderResult:
    exit_price: float
    exit_reason: str
    bars_held: int
    r: float
    pnl_usd: float
    fees_usd: float
    reached_target: bool = False


def simulate_ladder(
    *, side: str, entry: float, stop: float, target: float, quantity: float,
    path: list[dict], atr_at: list[float], interval: str, market_type: str = "futures",
) -> LadderResult:
    """Run the REAL exit ladder over a 1m path and return what the trade made.

    `evaluate_exit` is imported, not reimplemented — the ratchet-only invariant, the breakeven
    floor and the trail all have to be the ones that actually run in production.

    Driven on 1m CLOSES because that is what the live tick loop sees. A stop is therefore filled
    at the close that breached it, not at the stop level, which reproduces the adverse fill the
    live system really gets (measured at ~39 points past the stop over the paper week) instead of
    pretending stops fill exactly.

    `atr_at[k]` is the ATR in force while walking path[k] — the value from the most recently
    completed interval bar, matching the executor's per-bar ATR cache.
    """
    step = bucket_ms(interval)
    position = {"side": side, "entryPrice": entry, "stopLoss": stop, "takeProfit": target,
                "initialStop": stop, "lifecycle": "open", "extremePrice": entry}
    start_ms = path[0]["timestamp"] if path else 0
    exit_price, reason = entry, "no_path"

    for k, bar in enumerate(path):
        price = bar["close"]
        bars_held = int((bar["timestamp"] - start_ms) // step)
        action = evaluate_exit(position, price, atr_at[k], bars_held=bars_held)
        if action.is_exit:
            exit_price, reason = price, action.reason
            break
        if action.is_ratchet and action.new_stop is not None:
            position["stopLoss"] = action.new_stop
            position["lifecycle"] = action.new_lifecycle or position["lifecycle"]
        position["extremePrice"] = better_extreme(side, position["extremePrice"], price)
    else:
        # Ran out of data before the ladder resolved. Marked so it can be excluded rather than
        # silently counted as a flat trade.
        exit_price, reason = (path[-1]["close"] if path else entry), "truncated"
        bars_held = int((path[-1]["timestamp"] - start_ms) // step) if path else 0

    fill = slipped_fill(exit_price, side, opening=False)
    gross = (fill - entry) * quantity if side == LONG else (entry - fill) * quantity
    fees = (entry * quantity + fill * quantity) * taker_fee_pct(market_type)
    return LadderResult(
        exit_price=fill, exit_reason=_reason_kind(reason, position["lifecycle"]),
        bars_held=bars_held, reached_target=position["lifecycle"] == RUNNER,
        r=r_multiple(position, fill), pnl_usd=gross - fees, fees_usd=fees,
    )


def _reason_kind(reason: str, lifecycle: str) -> str:
    """Collapse the ladder's prose into a countable category.

    A trailing exit and a stop-out both come back as "Stop-loss hit at ..." — the ladder makes no
    distinction because mechanically there is none. For a report they are opposite outcomes (one
    is a winner being banked, the other the trade being wrong), so the lifecycle is what separates
    them: only a position that crossed its target is ever in RUNNER.
    """
    if reason.startswith("Stop-loss"):
        return "trail_exit" if lifecycle == RUNNER else "stop"
    if reason.startswith("Max hold"):
        return "max_hold_runner" if lifecycle == RUNNER else "max_hold"
    if reason == "truncated":
        return "truncated"
    return reason


# --------------------------------------------------------------------------- replay


@dataclass
class Observation:
    """One (bar, side) measurement."""
    bar_time_ms: int
    cohort: str          # 'signal' | 'inverse' | 'random'
    trigger: str
    side: str
    regime: str
    adx: float
    atr_pct: float
    close: float
    forward: dict = field(default_factory=dict)   # horizon bars -> signed % return
    r: Optional[float] = None
    pnl_usd: Optional[float] = None
    fees_usd: float = 0.0
    # 'no_path' until a ladder actually runs — the bar sits too close to the end of the data to
    # be simulated. Distinct from 'truncated' (a ladder ran but never resolved) and never blank,
    # so an unsimulated bar cannot be mistaken for a flat outcome in the exit histogram.
    exit_reason: str = "no_path"
    bars_held: int = 0
    reached_target: bool = False


@dataclass
class ReplayResult:
    symbol: str
    interval: str
    start: str
    end: str
    mode: str
    bars_evaluated: int = 0
    gate_calls: int = 0
    gate_skips: dict = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)


def walk_gate(
    bars: list[dict], *, symbol: str, interval: str, candle_limit: int,
    mode: str = "signals", progress_every: int = 5000,
) -> tuple[list[tuple[int, dict, dict]], list[float], dict, int]:
    """Run the real feature engine + gate over every bar.

    Returns (approved, atr_by_bar, gate_skips, bars_evaluated), where `approved` is one entry per
    (bar, trigger) the gate would have paid an LLM call for.

    Factored out of `replay` so the LLM screen walks the history exactly the same way. Two code
    paths deciding which bars are "gate-approved" would be two different experiments.
    """
    atr_by_bar: list[float] = [0.0] * len(bars)
    approved: list[tuple[int, dict, dict]] = []
    skips: dict = {}
    evaluated = 0
    open_until = -1        # sequential mode: index the current position frees up at
    last_close_idx = -10**9

    for i in range(candle_limit, len(bars)):
        state = build_feature_state(symbol, symbol, interval, bars[i - candle_limit:i + 1])
        atr = state.summary.get("atr14") or 0.0
        atr_by_bar[i] = 0.0 if math.isnan(atr) else float(atr)
        evaluated += 1
        if progress_every and evaluated % progress_every == 0:
            log.info("  ...%d bars evaluated (%s)", evaluated, _iso(bars[i]["timestamp"]))

        if mode == "sequential" and i < open_until:
            continue
        bars_since = (i - last_close_idx) if last_close_idx > -10**8 else None
        gate = evaluate_gate(state, None,
                             bars_since_last_close=bars_since if mode == "sequential" else None)
        if not gate.should_call or gate.kind != ENTRY:
            skips[_skip_key(gate.reason)] = skips.get(_skip_key(gate.reason), 0) + 1
            continue

        atr_pct = (state.summary.get("atrPct") or 0.0) / 100.0
        if atr_pct <= 0 or math.isnan(atr_pct):
            continue
        for trig in gate.triggers:
            approved.append((i, {"regime": state.regime["label"], "adx": state.regime["adx"],
                                 "atr_pct": atr_pct, "close": state.close}, trig))
            if mode == "sequential":
                open_until, last_close_idx = i + MAX_HOLD_BARS, i + COOLDOWN_BARS
                break   # one position at a time
    return approved, atr_by_bar, skips, evaluated


def _stop_and_target(side: str, close: float, atr_pct: float) -> tuple[float, float]:
    """The protective levels policy would apply with no model opinion available.

    Uses the same clamp the live sizer uses, fed the floor as the request — which is what a model
    stop tighter than policy becomes anyway, and is the most conservative honest assumption when
    there is no model in the loop.
    """
    stop_pct, _ = clamp_stop_pct(MIN_STOP_ATR * atr_pct, atr_pct)
    stop = close * (1 - stop_pct) if side == LONG else close * (1 + stop_pct)
    risk = abs(close - stop)
    target = close + risk * MIN_RISK_REWARD if side == LONG else close - risk * MIN_RISK_REWARD
    return stop, target


def replay(
    bars_1m: list[dict],
    *,
    symbol: str = "BTCUSDT",
    interval: Optional[str] = None,
    candle_limit: Optional[int] = None,
    mode: str = "signals",
    random_baseline: int = 500,
    seed: int = 7,
    progress_every: int = 5000,
) -> ReplayResult:
    """Walk the history bar by bar through the real feature engine and gate.

    mode='signals'    — every gate-approved bar is measured independently. Maximum statistical
                        power, and the right mode for "does this trigger have an edge".
    mode='sequential' — one position at a time, with the live cooldown. Answers the different
                        question "what would the system have made", at a fraction of the sample.
    """
    interval = interval or config.pattern_interval
    candle_limit = candle_limit or config.pattern_candle_limit
    bars = rollup(bars_1m, interval)
    step = bucket_ms(interval)
    # A sorted timestamp array + bisect, NOT a {timestamp: index} dict. At six years of 1m bars
    # that dict is 3.2M entries and a few hundred MB of pure lookup overhead, for a lookup that a
    # binary search over an array the caller already needs does just as well.
    times_1m = [b["timestamp"] for b in bars_1m]

    result = ReplayResult(
        symbol=symbol, interval=interval, mode=mode,
        start=_iso(bars[0]["timestamp"]) if bars else "",
        end=_iso(bars[-1]["timestamp"]) if bars else "",
    )
    if len(bars) <= candle_limit:
        log.warning("Only %d %s bars — need more than the %d-bar warm-up window.",
                    len(bars), interval, candle_limit)
        return result

    # --- Pass 1: features + gate over every bar.
    approved, atr_by_bar, skips, evaluated = walk_gate(
        bars, symbol=symbol, interval=interval, candle_limit=candle_limit, mode=mode,
        progress_every=progress_every)
    result.gate_skips = skips
    result.bars_evaluated = evaluated
    result.gate_calls = len({i for i, _, _ in approved})

    # --- Pass 2: forward returns + ladder for each approved (bar, side).
    rng = random.Random(seed)
    for i, ctx, trig in approved:
        result.observations.append(
            _measure(bars, bars_1m, times_1m, atr_by_bar, i, ctx, trig["side"], trig["name"],
                     "signal", interval, step))
        if mode == "signals":
            # The same bar taken the OTHER way. A trigger with a real edge should beat its own
            # inverse; one that does not is telling you the bar, not the direction, was selected.
            result.observations.append(
                _measure(bars, bars_1m, times_1m, atr_by_bar, i, ctx, _flip(trig["side"]),
                         trig["name"], "inverse", interval, step))

    # --- Pass 3: a random-bar, random-side baseline. Without it there is nothing to say whether
    # an expectancy of -0.05R is bad or simply what this ladder does on this instrument.
    if random_baseline:
        candidates = [i for i in range(candle_limit, len(bars)) if atr_by_bar[i] > 0]
        for i in rng.sample(candidates, min(random_baseline, len(candidates))):
            atr_pct = atr_by_bar[i] / bars[i]["close"]
            ctx = {"regime": "n/a", "adx": float("nan"), "atr_pct": atr_pct, "close": bars[i]["close"]}
            result.observations.append(
                _measure(bars, bars_1m, times_1m, atr_by_bar, i, ctx, rng.choice([LONG, SHORT]),
                         "random", "random", interval, step))
    return result


def _flip(side: str) -> str:
    return SHORT if side == LONG else LONG


_SKIP_NUMBERS = re.compile(r"[-+]?\d*\.?\d+")


def _skip_key(reason: str) -> str:
    """Group gate refusals by KIND, not by wording.

    Every refusal quotes its own numbers ("ADX 40.6 ...", "Cooldown: 1 of 2 bars ..."), so keying
    on the raw string produced one bucket per distinct ADX reading and buried the actual
    distribution under hundreds of one-count rows.
    """
    return _SKIP_NUMBERS.sub("#", reason).split("(")[0].strip()[:60] or "no reason given"


def _index_at(times_1m: list[int], ms: int) -> Optional[int]:
    """Position of the 1m bar opening exactly at `ms`, or None. Binary search over sorted times."""
    k = bisect.bisect_left(times_1m, ms)
    return k if k < len(times_1m) and times_1m[k] == ms else None


def _measure(bars, bars_1m, times_1m, atr_by_bar, i, ctx, side, trigger, cohort, interval, step,
             levels: Optional[tuple[float, float]] = None) -> Observation:
    close = ctx["close"]
    obs = Observation(
        bar_time_ms=bars[i]["timestamp"], cohort=cohort, trigger=trigger, side=side,
        regime=ctx["regime"], adx=ctx["adx"], atr_pct=ctx["atr_pct"], close=close,
    )
    # Forward returns, signed so positive always means "this direction was right".
    for h in FORWARD_HORIZONS:
        if i + h < len(bars):
            raw = (bars[i + h]["close"] - close) / close * 100.0
            obs.forward[h] = raw if side == LONG else -raw

    # The ladder starts on the bar AFTER the decision bar closes.
    entry_ms = bars[i]["timestamp"] + step
    start = _index_at(times_1m, entry_ms)
    if start is None:
        return obs
    # `levels` lets a caller substitute a stop/target the MODEL chose. Left None everywhere in the
    # replay itself: holding stop placement fixed is what makes a cohort comparison measure the
    # one thing it names, rather than selection and stop-placement mixed together.
    stop, target = levels if levels else _stop_and_target(side, close, ctx["atr_pct"])
    entry_fill = slipped_fill(close, side, opening=True)
    quantity = NOTIONAL_USD / entry_fill

    # Cap the path at the ladder's own max hold so a trade cannot silently run for years.
    horizon_1m = MAX_HOLD_BARS * (step // MINUTE_MS) + 2
    path = bars_1m[start:start + horizon_1m]
    if not path:
        return obs
    atr_path = _atr_for_path(path, bars, atr_by_bar, i, step)
    r = simulate_ladder(side=side, entry=entry_fill, stop=stop, target=target, quantity=quantity,
                        path=path, atr_at=atr_path, interval=interval)
    obs.r, obs.pnl_usd, obs.fees_usd = r.r, r.pnl_usd, r.fees_usd
    obs.exit_reason, obs.bars_held, obs.reached_target = r.exit_reason, r.bars_held, r.reached_target
    return obs


def _atr_for_path(path, bars, atr_by_bar, decision_idx, step) -> list[float]:
    """ATR in force at each 1m step: the value from the most recently COMPLETED interval bar.

    Mirrors PatternExecutor._atr_by_ticker, which is refreshed on candle close and read on every
    tick — so a position opened at bar i trails on bar i's ATR until bar i+1 closes.
    """
    out, j = [], decision_idx
    for bar in path:
        while j + 1 < len(bars) and bars[j + 1]["timestamp"] + step <= bar["timestamp"]:
            j += 1
        out.append(atr_by_bar[j])
    return out


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- reporting


def _resolved(rows: list[Observation]) -> list[Observation]:
    """Trades the ladder actually finished. 'truncated' (ran out of data) and 'no_path' (too near
    the end to simulate) are excluded — counting either as a flat outcome would pull every
    expectancy toward zero."""
    return [o for o in rows if o.r is not None and o.exit_reason not in ("truncated", "no_path")]


def _agg(rows: list[Observation]) -> dict:
    done = _resolved(rows)
    rs = [o.r for o in done]
    pnl = [o.pnl_usd for o in done]
    fwd = {h: [o.forward[h] for o in rows if h in o.forward] for h in FORWARD_HORIZONS}
    out = {
        "n": len(rows),
        "n_resolved": len(rs),
        "expectancy_r": stats.mean(rs) if rs else float("nan"),
        "win_rate": (sum(1 for r in rs if r > 0) / len(rs) * 100) if rs else float("nan"),
        # The diagnostic that explained the paper week: with a 1.5R target and a trailing runner,
        # the system only makes money when trades REACH the target. Live that was 4 of 41 (10%).
        "target_rate": (sum(1 for o in done if o.reached_target) / len(done) * 100) if done else float("nan"),
        "total_pnl": sum(pnl),
        "fees": sum(o.fees_usd for o in rows),
        "t_stat": _t(rs),
        "r_values": rs,
        "fwd": {h: (stats.mean(v) if v else float("nan")) for h, v in fwd.items()},
    }
    gross_win = sum(p for p in pnl if p > 0)
    gross_loss = abs(sum(p for p in pnl if p <= 0))
    out["profit_factor"] = (gross_win / gross_loss) if gross_loss else float("inf")
    return out


def _welch_t(a: list[float], b: list[float]) -> float:
    """Two-sample t for 'is a's mean different from b's', unequal variances.

    The signal-vs-inverse comparison is the sharper test: both cohorts trade the same bars with
    the same ladder and pay the same fees, so everything except DIRECTION cancels out. A signal
    can look flat against zero purely because costs sit on top of a real edge, and still beat its
    own inverse decisively.
    """
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    va, vb = stats.variance(a) / len(a), stats.variance(b) / len(b)
    return (stats.mean(a) - stats.mean(b)) / math.sqrt(va + vb) if (va + vb) > 0 else float("nan")


def _t(values: list[float]) -> float:
    """t-statistic of the mean against zero. |t| >= 2 is the usual bar for 'not obviously noise'."""
    if len(values) < 2:
        return float("nan")
    sd = stats.stdev(values)
    return (stats.mean(values) / sd * math.sqrt(len(values))) if sd else float("nan")


def _fmt(v: float, nd: int = 3, width: int = 8) -> str:
    return f"{'—':>{width}}" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:>{width}.{nd}f}"


def report(result: ReplayResult) -> str:
    """A human-readable verdict. The numbers that matter are expectancy in R and its t-stat."""
    obs = result.observations
    lines = [
        "",
        f"REPLAY  {result.symbol} {result.interval}  {result.start} -> {result.end}  (mode: {result.mode})",
        "=" * 100,
        f"bars evaluated: {result.bars_evaluated:,}    gate-approved: {result.gate_calls:,} "
        f"({result.gate_calls / max(result.bars_evaluated, 1) * 100:.1f}% of bars)",
        "",
        "Why the gate declined (top reasons):",
    ]
    for reason, n in sorted(result.gate_skips.items(), key=lambda kv: -kv[1])[:6]:
        lines.append(f"  {n:>8,}  {reason}")

    signal = [o for o in obs if o.cohort == "signal"]
    if not signal:
        lines += ["", "No signals in this window — nothing to measure."]
        return "\n".join(lines)

    hdr = (f"\n{'cohort / trigger':<28}{'n':>7}{'exp R':>9}{'t':>7}{'win%':>7}{'tgt%':>7}{'PF':>7}"
           f"{'net $':>11}{'fees $':>10}" + "".join(f"{f'fwd{h}':>9}" for h in FORWARD_HORIZONS))

    def row(label: str, rows: list[Observation]) -> str:
        a = _agg(rows)
        return (f"{label:<28}{a['n']:>7,}{_fmt(a['expectancy_r'], 3, 9)}{_fmt(a['t_stat'], 1, 7)}"
                f"{_fmt(a['win_rate'], 1, 7)}{_fmt(a['target_rate'], 1, 7)}"
                f"{_fmt(a['profit_factor'], 2, 7)}"
                f"{a['total_pnl']:>11,.0f}{a['fees']:>10,.0f}"
                + "".join(_fmt(a['fwd'][h], 3, 9) for h in FORWARD_HORIZONS))

    lines += ["", "COHORTS — the signal against its own inverse and against random entries", hdr,
              "-" * len(hdr.strip())]
    for cohort in ("signal", "inverse", "random"):
        rows = [o for o in obs if o.cohort == cohort]
        if rows:
            lines.append(row(cohort.upper(), rows))

    lines += ["", "BY TRIGGER (signal cohort only)", hdr, "-" * len(hdr.strip())]
    for name in sorted({o.trigger for o in signal}):
        lines.append(row(name, [o for o in signal if o.trigger == name]))

    lines += ["", "BY REGIME (signal cohort only)", hdr, "-" * len(hdr.strip())]
    for name in sorted({o.regime for o in signal}):
        lines.append(row(name, [o for o in signal if o.regime == name]))

    exits: dict[str, int] = {}
    for o in signal:
        exits[o.exit_reason] = exits.get(o.exit_reason, 0) + 1
    lines += ["", "HOW SIGNAL TRADES ENDED:  "
              + "  ".join(f"{k}={v}" for k, v in sorted(exits.items(), key=lambda kv: -kv[1]))]

    lines += ["", "VERDICT", "-" * 70]
    lines.append(_verdict(_agg(signal),
                          _agg([o for o in obs if o.cohort == "inverse"]),
                          _agg([o for o in obs if o.cohort == "random"]),
                          mode=result.mode))
    return "\n".join(lines)


def _verdict(sig: dict, inv: dict, rnd: dict, *, mode: str = "signals") -> str:
    """Two questions, deliberately kept apart.

      1. Is the signal PROFITABLE?  expectancy vs zero — this includes the cost of trading.
      2. Does the signal have DIRECTIONAL EDGE?  signal vs its own inverse — costs cancel.

    They can disagree, and the disagreement is the useful case: an edge that exists but is
    smaller than the fees says "keep the triggers, cut the cost or the trade rate", which is a
    completely different instruction from "the triggers are noise".
    """
    if sig["n_resolved"] < 100:
        return (f"Only {sig['n_resolved']} resolved trades — too few to conclude anything. "
                "Widen the date range.")

    e, t = sig["expectancy_r"], sig["t_stat"]
    out = [f"1. PROFITABLE?  expectancy {e:+.4f}R over {sig['n_resolved']:,} trades (t = {t:+.1f}), "
           f"net {sig['total_pnl']:+,.0f} USD after {sig['fees']:,.0f} of fees."]
    if abs(t) < 2:
        out.append("   Not distinguishable from break-even after costs.")
    elif e < 0:
        out.append("   Significantly UNPROFITABLE as it stands.")
    else:
        out.append("   Significantly profitable.")

    out.append("")
    if inv["n_resolved"] < 100:
        out.append("2. DIRECTIONAL EDGE?  not measured — sequential mode takes one position at a "
                   "time, so there is no inverse cohort to compare against. Use --mode signals "
                   "for the directional test.")
    else:
        tt = _welch_t(sig["r_values"], inv["r_values"])
        out.append(f"2. DIRECTIONAL EDGE?  signal {e:+.4f}R vs its inverse "
                   f"{inv['expectancy_r']:+.4f}R (two-sample t = {tt:+.1f}); "
                   f"random baseline {rnd['expectancy_r']:+.4f}R.")
        if math.isnan(tt) or abs(tt) < 2:
            out.append("   No measurable edge. Taking these bars the other way does just as well, "
                       "so the triggers are selecting VOLATILITY, not DIRECTION. Prompt work "
                       "cannot fix that — the trigger set itself is what needs replacing.")
        elif tt < 0:
            out.append("   The signal is significantly WORSE than its inverse. The triggers carry "
                       "real directional information and are pointing the wrong way.")
        else:
            out.append("   Real directional edge: the signal beats its own inverse significantly.")
            if abs(t) < 2 or e < 0:
                out.append("   But (1) says it does not survive costs. The edge is real and "
                           "smaller than the fees — so the lever is cost and trade frequency "
                           "(fewer, higher-conviction entries; a longer interval), not the "
                           "triggers. This is where the model's SKIP is worth money.")

    out.append("")
    out.append(f"Target reached on {sig['target_rate']:.1f}% of trades. The ladder only pays when "
               f"a trade reaches its target and trails; below ~{100 / (1 + MIN_RISK_REWARD):.0f}% "
               "the stops outweigh the runners.")
    out.append("")
    if mode == "signals":
        out.append("CAVEAT: in 'signals' mode trades OVERLAP — neighbouring bars trigger on the "
                   "same move and share a price path, so the observations are not independent "
                   "and the t-statistics above are OPTIMISTIC (further from zero than they "
                   "should be). Treat them as an upper bound on significance; a borderline |t| "
                   "near 2 is not a result. Re-run with --mode sequential for non-overlapping "
                   "trades and an honest t.")
    else:
        out.append("Sequential mode: one position at a time, so these trades do not overlap and "
                   "the t-statistics are honest — at the cost of a much smaller sample.")
    return "\n".join(out)


# --------------------------------------------------------------------------- CLI


async def _main(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end
           else datetime.now(timezone.utc))
    log.info("Loading 1m bars for %s from %s to %s...", args.symbol, start.date(), end.date())
    bars_1m = await load_1m(args.symbol, start, end)
    if not bars_1m:
        log.error("No stored bars in that range. Run the backfill first.")
        return 1
    log.info("Loaded %s 1m bars. Replaying...", f"{len(bars_1m):,}")

    result = replay(bars_1m, symbol=args.symbol, interval=args.interval,
                    candle_limit=args.candle_limit, mode=args.mode,
                    random_baseline=args.random_baseline)
    print(report(result))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({**{k: v for k, v in asdict(result).items() if k != "observations"},
                       "observations": [asdict(o) for o in result.observations]}, fh, indent=2)
        log.info("Wrote %s", args.json)
    return 0


def run() -> None:
    p = argparse.ArgumentParser(description="Replay the pattern brain's deterministic layer over history.")
    p.add_argument("--symbol", default=config.backfill_symbol)
    p.add_argument("--interval", default=config.pattern_interval)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: now)")
    p.add_argument("--candle-limit", type=int, default=config.pattern_candle_limit)
    p.add_argument("--mode", choices=("signals", "sequential"), default="signals")
    p.add_argument("--random-baseline", type=int, default=500)
    p.add_argument("--json", default=None, help="also write every observation to this file")
    raise SystemExit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    run()
