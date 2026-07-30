"""Prompt construction.

Two principles, both learned the expensive way on the strategy brain:

  * Give the model LEVELS, not verdicts. "double_top" cannot be turned into a stop; "resistance
    64430.5, 13 touches, 2.1 ATR above" can. Pattern names are supporting evidence, not payload.
  * State the constraints the validator will enforce, in the prompt. The validator rejects and
    never rewrites, so a model that does not know the rules simply produces rejected decisions
    and burns money for nothing.

The failure memory is deliberately short and compressed. The operator's concern was real: a
couple of unlucky trades in one setup should not teach the model to avoid that setup forever, so
it sees only the last few losses, as facts rather than as instructions.
"""
from __future__ import annotations

import json
from typing import Optional

from .sizing import MAX_STOP_PCT, MIN_STOP_PCT

MIN_RISK_REWARD = 1.5
MAX_FAILURES_SHOWN = 5


def _levels_block(levels: dict) -> str:
    def fmt(rows, label):
        if not rows:
            return f"  {label}: none within range"
        return f"  {label}:\n" + "\n".join(
            f"    - {r['price']:.2f}  ({r['touches']} touches, {r['distAtr']:.2f} ATR away)"
            for r in rows
        )

    parts = [fmt(levels.get("resistance", []), "Resistance above"),
             fmt(levels.get("support", []), "Support below")]
    if levels.get("rangeHigh") is not None and levels.get("rangeLow") is not None:
        parts.append(f"  Range: {levels['rangeLow']:.2f} - {levels['rangeHigh']:.2f}")
    return "\n".join(parts)


def _patterns_block(patterns: dict) -> str:
    chart = patterns.get("chart") or []
    candle = patterns.get("candle") or []
    lines = []
    for p in chart:
        extra = f", neckline {p['neckline']:.2f}" if p.get("neckline") is not None else ""
        lines.append(f"    - {p['name']} ({p['direction']}) at {p.get('level', 0):.2f}{extra}")
    for p in candle:
        lines.append(f"    - {p['name']} ({p['direction']}, {p['barsAgo']} bars ago)")
    return "\n".join(lines) if lines else "    - none"


def _failures_block(failures: list[dict]) -> str:
    if not failures:
        return "  No recent losing trades."
    lines = []
    for f in failures[:MAX_FAILURES_SHOWN]:
        lines.append(
            f"    - {f.get('side')} on {f.get('trigger', 'unknown trigger')} in a "
            f"{f.get('regime', 'unknown')} regime: {f.get('rMultiple', 0):.2f}R "
            f"after {f.get('barsHeld', 0)} bars ({f.get('exitReason', 'unknown exit')})"
        )
    return "\n".join(lines)


def build_entry_prompt(
    state_pack: dict,
    account: dict,
    triggers: list[dict],
    failures: Optional[list[dict]] = None,
) -> str:
    """Prompt for the flat -> maybe-enter decision."""
    market = state_pack["market"]
    regime = state_pack["regime"]
    ind = state_pack["indicators"]
    close = market["close"]

    def num(v, digits=2):
        return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "unavailable"

    return f"""You are a disciplined intraday trading analyst. Decide whether to OPEN a position on \
{market['symbol']} using the market read below. Respond with JSON only.

MARKET ({market['interval']} bar close)
  Price: {num(close)}
  Regime: {regime['label']} (ADX {num(regime.get('adx'), 1)}, EMA stack {regime.get('emaStack')})
  RSI 14: {num(ind.get('rsi14'), 1)}
  ATR 14: {num(ind.get('atr14'))} ({num(ind.get('atrPct'), 3)}% of price)
  EMAs: 20={num(ind.get('ema20'))} 50={num(ind.get('ema50'))} 200={num(ind.get('ema200'))}

LEVELS
{_levels_block(state_pack.get('levels', {}))}

STRUCTURE
  Last swing high: {num(state_pack.get('structure', {}).get('lastSwingHigh'))}
  Last swing low:  {num(state_pack.get('structure', {}).get('lastSwingLow'))}
  Higher highs: {state_pack.get('structure', {}).get('higherHighs')} | \
Higher lows: {state_pack.get('structure', {}).get('higherLows')}

PATTERNS
{_patterns_block(state_pack.get('patterns', {}))}

WHAT FIRED ON THIS BAR
{chr(10).join(f"    - {t['name']} ({t['side']}): {t['detail']}" for t in triggers)}

ACCOUNT
  Day-start balance: {num(account.get('dayStartBalance'))} USDT
  Lost so far today: {num(account.get('realizedLossToday'))} USDT
  Remaining daily risk budget: {num(account.get('remainingRiskUsd'))} USDT

RECENT LOSING TRADES (context only — do not over-fit to these; a setup that failed twice may \
still be valid)
{_failures_block(failures or [])}

HARD CONSTRAINTS — a decision breaking any of these is rejected and the trade is skipped:
  1. Trade only WITH the regime: long in an uptrend, short in a downtrend. Never counter-trend.
  2. The stop must be between {MIN_STOP_PCT * 100:.2f}% and {MAX_STOP_PCT * 100:.2f}% from entry,
     and at least 0.5 ATR away.
  3. Reward:risk must be at least {MIN_RISK_REWARD}:1 against the stop.
  4. Give an entry RANGE (entry_min/entry_max) that brackets the current price — roughly
     +/- 0.25 ATR. Price moves between this decision and the fill; a range that excludes the
     current price will simply never fill.
  5. Stop below entry for a long, above entry for a short.
  6. If the setup is not clean, answer SKIP. Skipping is free; a bad entry is not.

Do NOT propose a position size. Sizing is computed from the risk budget, not chosen by you.

Return JSON matching this shape exactly:
{json.dumps({
    "action": "ENTRY or SKIP",
    "side": "long or short (null when SKIP)",
    "entry_min": "number or null",
    "entry_max": "number or null",
    "stop_loss": "number or null",
    "take_profit": "number or null",
    "confidence": "0 to 1",
    "reasoning": "one or two sentences citing specific levels",
}, indent=2)}"""


def build_exit_prompt(state_pack: dict, position: dict, triggers: list[dict]) -> str:
    """Prompt for the should-we-close-early decision."""
    market = state_pack["market"]
    regime = state_pack["regime"]
    ind = state_pack["indicators"]
    close = market["close"]
    entry = float(position.get("entryPrice") or 0)
    side = position.get("side") or "long"
    pnl_pct = ((close - entry) / entry * 100) if side == "long" else ((entry - close) / entry * 100)

    def num(v, digits=2):
        return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "unavailable"

    return f"""You are a disciplined intraday trading analyst. An open position exists. Decide \
whether to CLOSE IT EARLY. Respond with JSON only.

POSITION
  Side: {side}
  Entry: {num(entry)}
  Current price: {num(close)}
  Unrealized: {pnl_pct:+.2f}%
  Bars held: {position.get('barsHeld', 'unknown')}

MARKET ({market['interval']} bar close)
  Regime: {regime['label']} (ADX {num(regime.get('adx'), 1)}, EMA stack {regime.get('emaStack')})
  RSI 14: {num(ind.get('rsi14'), 1)}
  ATR 14: {num(ind.get('atr14'))} ({num(ind.get('atrPct'), 3)}%)

LEVELS
{_levels_block(state_pack.get('levels', {}))}

PATTERNS
{_patterns_block(state_pack.get('patterns', {}))}

WHAT FIRED AGAINST THIS POSITION
{chr(10).join(f"    - {t['name']} ({t['side']}): {t['detail']}" for t in triggers) or "    - nothing specific"}

YOUR SCOPE IS LIMITED:
  * You may only bring the exit FORWARD. A protective stop is already in place and is enforced
    deterministically — you cannot cancel it, widen it, or ask to hold past it.
  * Answer EXIT only if the market read has genuinely turned against the position: structure
    broken, regime flipped, or momentum clearly exhausted at a level.
  * Answer HOLD if the move is merely pausing. Letting a winner run is the default.

Return JSON matching this shape exactly:
{json.dumps({"action": "EXIT or HOLD", "reasoning": "one sentence citing what changed"}, indent=2)}"""
