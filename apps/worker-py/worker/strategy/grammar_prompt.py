"""DSL grammar spec + generation-prompt assembly — port of
apps/backend/src/modules/strategy/dsl/grammar-prompt.ts + the summaryPrompt built in
strategy-engine.service.ts::generateStrategyForTicker. Kept in one place so the building blocks the
interpreter supports can't drift from indicators.py / ir.py.
"""
from __future__ import annotations

STRATEGY_GRAMMAR_PROMPT = (
    'You compose an automated LONG-ONLY trading strategy for a Binance SPOT market (you can only buy '
    'then sell — no shorting). A strategy is a rule tree built ONLY from the blocks below. Your JSON '
    'response has these fields: "strategyName" (string), "reasoning" (string), "entry" (a condition '
    'tree), "exit" (a condition tree), and "risk" (the risk object). entry/exit/risk are JSON objects '
    '(NOT strings).\n'
    '\n'
    'OPERANDS (a numeric value at each bar):\n'
    '- {"op":"price","field":"open|high|low|close|volume"}\n'
    '- {"op":"const","value":<number>}\n'
    '- {"op":"indicator","kind":"ema|sma|rsi","period":<int 2-400>}            // moving averages / RSI on close\n'
    '- {"op":"indicator","kind":"macd","fast":<int>,"slow":<int>,"signal":<int>,"field":"line|signal|hist"}  // fast<slow\n'
    '- {"op":"indicator","kind":"atr","period":<int>}                          // average true range (volatility)\n'
    '- {"op":"indicator","kind":"adx","period":<int>}                          // trend strength 0-100 (>25 trending)\n'
    '- {"op":"indicator","kind":"bollinger","period":<int>,"mult":<0-5>,"field":"upper|mid|lower"}\n'
    '- {"op":"indicator","kind":"donchian","period":<int>,"field":"upper|lower"} // channel of highs/lows\n'
    '- {"op":"indicator","kind":"stochastic","period":<int>,"smoothK":<int>,"smoothD":<int>,"field":"k|d"}\n'
    '- {"op":"indicator","kind":"rollingHigh|rollingLow","period":<int>}       // N-bar high/low\n'
    '\n'
    'CONDITIONS (true/false at each bar):\n'
    '- {"op":"gt|lt|gte|lte","left":<operand>,"right":<operand>}\n'
    '- {"op":"crossAbove|crossBelow","left":<operand>,"right":<operand>}       // left crosses over/under right this bar\n'
    '- {"op":"and|or","conditions":[<condition>, ...]}                          // 1-8 children\n'
    '- {"op":"not","condition":<condition>}\n'
    '\n'
    'RISK (always active alongside the exit rule): {"stopLossPct":<0.1-20>,"takeProfitPct":<0.1-50>, '
    'optional "trailingStopPct":<0.1-50>, optional "maxHoldBars":<int>}\n'
    '\n'
    'EXIT PHILOSOPHY (important — the system protects winners for you):\n'
    '- takeProfitPct is a SOFT target by default: when price reaches it the position is NOT closed; '
    'instead a tight trailing stop rides the trend for as much profit as it will give, and only exits '
    'on a pullback from the peak. So pick takeProfitPct as "a level a real move on this ticker/timeframe '
    'can reach", not as a cap — bigger genuine trends will earn more than the number.\n'
    '- The system ALSO auto-applies, on every strategy: a profit-protection floor (once a trade is up a '
    'couple of percent it can never round-trip back into a loss) and a short minimum-hold that blocks '
    'same-bar whipsaw exits. You do NOT set these and must NOT try to emulate them in your exit rule.\n'
    '- Because of the above, your "exit" rule should be a GENUINE TREND-STRUCTURE BREAK — the signal that '
    'the move is actually over — NOT a hair-trigger reversal. Good exits: close falls back below a slower '
    'trend EMA (e.g. close < ema(100/200)), price breaks a recent swing low (price.low < rollingLow), a '
    'momentum oscillator rolls over from strong to weak (rsi/stochastic crossing down through a mid level), '
    'or MACD histogram turning negative. AVOID using a FAST MA crossover as the exit (e.g. ema9 crossing '
    'below ema21): on noisy intraday bars it fires on every minor wiggle, closing trades for tiny '
    'gains/losses before any trend can develop — the single most common way these strategies bleed fees '
    'and never ride a winner.\n'
    '\n'
    'RULES:\n'
    '- "entry" opens a long when it is true and you are flat. "exit" closes the long when true. The risk '
    'block (stop-loss / soft take-profit / optional trailing stop / max-hold) ALWAYS applies on top of the '
    'exit rule, plus the auto profit-floor and min-hold described above.\n'
    '- Pick ANY approach that fits the market and the lessons — trend-following (MA crossovers, Donchian '
    'breakouts), mean-reversion (RSI/Stochastic oversold, Bollinger touch), or combinations. Do NOT default '
    'to an EMA crossover: it is only one option and is often not the best. Deliberately consider oscillator '
    '(RSI/Stochastic) and channel (Bollinger/Donchian) approaches, and favor variety over repeating the same '
    'MA-crossover shape.\n'
    '- Pair a responsive ENTRY with a patient, structural EXIT so winners are given room to run into the '
    'trailing stop rather than being cut on the first pullback.\n'
    '- Keep it PARSIMONIOUS to avoid overfitting: the count of DISTINCT tunable knobs (each distinct '
    'indicator counts by its params; an indicator reused in entry and exit counts once; each distinct const '
    'counts 1; plus stopLossPct + takeProfitPct + any optional risk knobs) must be <= the parameter budget '
    'stated below.\n'
    '- Round-trip fees are ~0.15%, so avoid rules that fire constantly on noise; give trades room to develop.\n'
    '- IMPORTANT window semantics: donchian.upper / rollingHigh INCLUDE the current bar, so "close > '
    'donchian.upper" (or rollingHigh) can NEVER be true. For an UPWARD breakout compare the bar\'s HIGH '
    '({"op":"price","field":"high"}), or use these channels for MEAN-REVERSION (buy near donchian.lower / '
    'rollingLow). Likewise close < donchian.lower can\'t fire — compare the LOW.\n'
    '- The strategy must actually trade: entries that stack many rare conditions (e.g. RSI<30 AND a deep '
    'trend filter AND a band touch) can produce too few trades to be evaluable. Aim for enough entries over '
    'the window to clear the minimum-trade requirement while keeping the profit factor healthy.\n'
    '\n'
    'EXAMPLE 1 — responsive EMA-crossover ENTRY with a patient trend-STRUCTURE-break exit (close falls back '
    'below the 50 EMA), so a winner rides instead of being cut on the first fast-EMA wiggle:\n'
    '{"entry":{"op":"and","conditions":[{"op":"crossAbove","left":{"op":"indicator","kind":"ema","period":20},'
    '"right":{"op":"indicator","kind":"ema","period":50}},{"op":"gt","left":{"op":"price","field":"close"},'
    '"right":{"op":"indicator","kind":"ema","period":200}}]},"exit":{"op":"lt","left":{"op":"price",'
    '"field":"close"},"right":{"op":"indicator","kind":"ema","period":50}},"risk":{"stopLossPct":3,'
    '"takeProfitPct":6}}\n'
    '\n'
    'EXAMPLE 2 — RSI mean-reversion, only in an uptrend:\n'
    '{"entry":{"op":"and","conditions":[{"op":"lt","left":{"op":"indicator","kind":"rsi","period":14},'
    '"right":{"op":"const","value":30}},{"op":"gt","left":{"op":"price","field":"close"},'
    '"right":{"op":"indicator","kind":"ema","period":200}}]},"exit":{"op":"gt","left":{"op":"indicator",'
    '"kind":"rsi","period":14},"right":{"op":"const","value":55}},"risk":{"stopLossPct":2,"takeProfitPct":4}}'
)


def _num(x: float) -> str:
    """Render a number the way JS `${Number(x)}` interpolation does (no trailing '.0')."""
    f = float(x)
    return str(int(f)) if f.is_integer() else repr(f)


def build_generation_prompt(
    symbol: str,
    interval: str,
    latest_price: float,
    param_budget: int,
    allow_short: bool,
    signal_emphasis: str,
    lessons_block: str,
) -> str:
    """Assemble the full DSL generation prompt — mirror of strategy-engine.service.ts summaryPrompt."""
    direction_clause = (
        'MARKET TYPE: FUTURES — you MAY compose a SHORT strategy instead of a long. To go short, set '
        '"direction":"short": "entry" then SELLS to open (the trade profits when price FALLS) and "exit" '
        'BUYS to close; stop-loss / take-profit / trailing / profit-floor all mirror automatically. In a '
        'clear downtrend a short is usually better than sitting flat. For a long, set "direction":"long" '
        'or omit it — pick whichever the market and lessons favor.'
        if allow_short
        else 'MARKET TYPE: SPOT — LONG ONLY. Do NOT set "direction":"short" (you can only buy then sell).'
    )
    emphasis_line = f"\nPlanner emphasis for this cycle (follow it): {signal_emphasis}\n" if signal_emphasis else ""
    return (
        f"Design an automated trading strategy for {symbol} (Interval: {interval}, Latest Price: {_num(latest_price)}).\n"
        f"\n"
        f"{STRATEGY_GRAMMAR_PROMPT}\n"
        f"\n"
        f"{direction_clause}\n"
        f"\n"
        f"Parameter budget for THIS strategy: at most {param_budget} distinct tunable knobs.\n"
        f"Timeframe: size indicator periods and stop/target to a {interval} bar — give trades room for a real "
        f"multi-bar swing rather than reacting to single-bar noise. Prioritize a positive out-of-sample Sharpe "
        f"(>= 1) and profit factor (>= 1.3) over trade frequency.\n"
        f"Move decisively away from any approach the lessons below show failing (low/negative Sharpe or profit "
        f"factor under 1) — do NOT repeat a documented failure; try a different indicator family or structure "
        f"when the lessons point that way.\n"
        f"{emphasis_line}"
        f"Relevant lessons from past strategies on this ticker/strategy type (steer away from documented "
        f"failures, keep successful approaches in mind):\n{lessons_block}\n"
        f"\n"
        f"Respond with strategyName, reasoning, and the entry, exit, and risk fields (JSON objects, not strings)."
    )
