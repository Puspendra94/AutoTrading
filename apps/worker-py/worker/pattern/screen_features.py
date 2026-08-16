"""Which candidate features actually predict the next move?

The replay harness answers "does the system as built have an edge" and the answer was no. This
answers the prior question: is there anything in the data worth building a system ON. It is the
cheap end of the loop — no trades simulated, no LLM, no exit ladder. Just: does this number, known
at bar close, carry information about what happens next.

WHY THIS EXISTS
Everything the pattern brain computes today is a function of OHLCV — RSI, ADX, EMA, ATR, chart
patterns, S/R levels. Measured over six years those carry no measurable direction on BTC, and more
indicators from the same five numbers is more of the same. The genuinely different information is
ORDER FLOW: who was aggressive. Two bars with identical OHLC can have opposite flow, so it is not
recoverable from price at all — which makes it the first thing worth testing.

WHAT IT MEASURES
The Information Coefficient: Spearman rank correlation between a feature at bar close and the
return over the following N bars. Rank rather than Pearson because a few 10-sigma crypto bars
would otherwise decide the answer on their own.

Interpreting IC, for daily-to-hourly horizons:
    |IC| < 0.01   noise
    |IC| ~ 0.02   marginal; real only with a very large sample
    |IC| ~ 0.05   genuinely useful
    |IC| > 0.10   suspicious — check for look-ahead before believing it

The t-statistic is what matters, not the raw IC: t = IC * sqrt(n). At n = 200k even IC 0.005 is
"significant" while being far too small to trade after fees. Both columns are printed for that
reason, along with the fee bar the feature would have to clear.

LOOK-AHEAD
Every feature is computed from bars up to and including bar i, and correlated with the return
STRICTLY AFTER i. A feature that peeks would show a huge IC — which is why anything above 0.10 is
reported as suspect rather than celebrated.

    python -m worker.pattern.screen_features --start 2021-01-01 --interval 15m
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

from ..config import config
from .replay import bucket_ms, load_1m, rollup

log = logging.getLogger("worker.pattern.screen_features")

# Forward horizons in bars.
HORIZONS = (1, 4, 16)


# --------------------------------------------------------------------------- features


def _safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    out = np.full_like(a, np.nan, dtype=np.float64)
    ok = np.isfinite(a) & np.isfinite(b) & (b != 0)
    out[ok] = a[ok] / b[ok]
    return out


def _roll_mean(x: np.ndarray, n: int) -> np.ndarray:
    """Trailing mean over n bars, NaN until there are n of them. Uses only past values."""
    out = np.full_like(x, np.nan, dtype=np.float64)
    if x.size < n:
        return out
    c = np.cumsum(np.nan_to_num(x, nan=0.0))
    cnt = np.cumsum(np.isfinite(x).astype(np.float64))
    sums = c[n - 1:] - np.concatenate(([0.0], c[:-n]))
    counts = cnt[n - 1:] - np.concatenate(([0.0], cnt[:-n]))
    # A window with NO finite values is UNKNOWN, not zero. Clamping the divisor to 1 turned an
    # empty window into a confident 0.0, which then flowed into buy_share_ma5 as a constant
    # -0.5 and showed up in the screen as coverage on data that did not exist.
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(counts > 0, sums / np.maximum(counts, 1.0), np.nan)
    out[n - 1:] = vals
    return out


def _zscore(x: np.ndarray, n: int) -> np.ndarray:
    """Trailing z-score — how unusual is this bar versus its own recent history."""
    m = _roll_mean(x, n)
    v = _roll_mean((x - m) ** 2, n)
    return _safe_div(x - m, np.sqrt(v))


def build_features(bars: list[dict]) -> dict[str, np.ndarray]:
    """Every candidate, aligned to bar index. All strictly backward-looking."""
    close = np.array([b["close"] for b in bars], dtype=np.float64)
    high = np.array([b["high"] for b in bars], dtype=np.float64)
    low = np.array([b["low"] for b in bars], dtype=np.float64)
    open_ = np.array([b["open"] for b in bars], dtype=np.float64)
    vol = np.array([b["volume"] for b in bars], dtype=np.float64)
    tbb = np.array([b.get("taker_buy_base") if b.get("taker_buy_base") is not None else np.nan
                    for b in bars], dtype=np.float64)
    trades = np.array([b.get("trades") if b.get("trades") is not None else np.nan
                       for b in bars], dtype=np.float64)

    f: dict[str, np.ndarray] = {}

    # --- ORDER FLOW. The new information: what share of volume lifted the offer. 0.5 is balanced.
    buy_share = _safe_div(tbb, vol)
    f["flow:buy_share"] = buy_share - 0.5
    f["flow:buy_share_z20"] = _zscore(buy_share, 20)
    f["flow:buy_share_ma5"] = _roll_mean(buy_share, 5) - 0.5
    # Signed flow imbalance scaled by size — a lopsided HEAVY bar means more than a lopsided thin one.
    f["flow:signed_vol_z20"] = _zscore((2.0 * buy_share - 1.0) * vol, 20)
    # Cumulative delta over 20 bars: sustained one-sided pressure rather than a single bar.
    delta = (2.0 * buy_share - 1.0) * vol
    f["flow:cvd20_z"] = _zscore(_roll_mean(delta, 20), 60)
    # Divergence: price down but buyers aggressive (or the reverse) — absorption.
    ret1 = np.concatenate(([np.nan], np.diff(close) / close[:-1]))
    f["flow:divergence"] = np.sign(np.nan_to_num(ret1)) * -1.0 * (buy_share - 0.5)
    # Average trade size: few big prints vs many small ones.
    f["flow:avg_trade_size_z"] = _zscore(_safe_div(vol, trades), 20)

    # --- PRICE / VOLUME baselines, so flow is judged against what is already available rather
    # than against zero. If these score the same, flow adds nothing.
    f["price:ret1"] = ret1
    f["price:ret5"] = np.concatenate(([np.nan] * 5, close[5:] / close[:-5] - 1.0))
    f["price:body_frac"] = _safe_div(close - open_, high - low)
    f["price:close_loc"] = _safe_div(close - low, high - low) - 0.5
    f["price:range_z20"] = _zscore(high - low, 20)
    f["vol:volume_z20"] = _zscore(vol, 20)
    return f


# --------------------------------------------------------------------------- scoring


def forward_returns(bars: list[dict], h: int) -> np.ndarray:
    """Return over the h bars STRICTLY AFTER each bar. NaN where it runs off the end."""
    close = np.array([b["close"] for b in bars], dtype=np.float64)
    out = np.full(close.size, np.nan)
    if close.size > h:
        out[:-h] = close[h:] / close[:-h] - 1.0
    return out


def _rank(x: np.ndarray) -> np.ndarray:
    order = x.argsort()
    r = np.empty_like(order, dtype=np.float64)
    r[order] = np.arange(x.size, dtype=np.float64)
    return r


def spearman(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    ok = np.isfinite(a) & np.isfinite(b)
    n = int(ok.sum())
    if n < 100:
        return float("nan"), n
    ra, rb = _rank(a[ok]), _rank(b[ok])
    ra -= ra.mean(); rb -= rb.mean()
    denom = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    return (float((ra * rb).sum()) / denom if denom else float("nan")), n


def screen(bars: list[dict], horizons=HORIZONS) -> list[dict]:
    feats = build_features(bars)
    fwd = {h: forward_returns(bars, h) for h in horizons}
    rows = []
    for name, values in feats.items():
        row = {"feature": name, "coverage": int(np.isfinite(values).sum())}
        for h in horizons:
            ic, n = spearman(values, fwd[h])
            row[f"ic{h}"] = ic
            row[f"t{h}"] = ic * math.sqrt(n) if n and not math.isnan(ic) else float("nan")
            row[f"n{h}"] = n
        rows.append(row)
    return rows


def report(rows: list[dict], interval: str, horizons=HORIZONS) -> str:
    lines = [
        "",
        f"FEATURE SCREEN — {interval}   Spearman IC vs forward return",
        "=" * 96,
        "IC is rank correlation: <0.01 noise, ~0.02 marginal, ~0.05 useful, >0.10 suspect "
        "(check look-ahead).",
        "t = IC*sqrt(n); with a large n a significant IC can still be far too small to trade.",
        "",
        f"{'feature':<26}{'coverage':>10}" + "".join(f"{f'IC@{h}':>10}{f't@{h}':>8}" for h in horizons),
        "-" * 96,
    ]
    def key(r):
        vals = [abs(r[f"ic{h}"]) for h in horizons if not math.isnan(r[f"ic{h}"])]
        return -max(vals) if vals else 0.0
    for r in sorted(rows, key=key):
        line = f"{r['feature']:<26}{r['coverage']:>10,}"
        for h in horizons:
            ic, t = r[f"ic{h}"], r[f"t{h}"]
            line += (f"{'—':>10}{'—':>8}" if math.isnan(ic) else f"{ic:>10.4f}{t:>8.1f}")
        lines.append(line)
    lines += ["", "Features prefixed 'flow:' come from taker-buy volume — information a candle "
              "does not contain. 'price:'/'vol:' are the existing OHLCV family, shown as the bar "
              "flow has to clear to be worth adding."]
    return "\n".join(lines)


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
    bars = rollup(bars_1m, args.interval)
    have_flow = sum(1 for b in bars if b.get("taker_buy_base"))
    log.info("%s %s bars (%s with flow data).", f"{len(bars):,}", args.interval, f"{have_flow:,}")
    if have_flow == 0:
        log.warning("No flow data in this range — run the backfill with from_start=True first.")
    print(report(screen(bars), args.interval))
    return 0


def run() -> None:
    p = argparse.ArgumentParser(description="Screen candidate features by information coefficient.")
    p.add_argument("--symbol", default=config.backfill_symbol)
    p.add_argument("--interval", default=config.pattern_interval)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None)
    raise SystemExit(asyncio.run(_main(p.parse_args())))


if __name__ == "__main__":
    run()
