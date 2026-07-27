/**
 * The DSL grammar spec injected into the strategy-generation prompt. Kept here (not inline in the
 * service) so the exact building blocks the interpreter supports are described in one place and
 * can't drift from indicators.ts / strategy-ir.ts. Two worked examples anchor the JSON shape for
 * every provider (including jsonMode/Bedrock models that ignore the response schema).
 */
export const STRATEGY_GRAMMAR_PROMPT = `You compose an automated LONG-ONLY trading strategy for a Binance SPOT market (you can only buy then sell — no shorting). A strategy is a rule tree built ONLY from the blocks below. Your JSON response has these fields: "strategyName" (string), "reasoning" (string), "entry" (a condition tree), "exit" (a condition tree), and "risk" (the risk object). entry/exit/risk are JSON objects (NOT strings).

OPERANDS (a numeric value at each bar):
- {"op":"price","field":"open|high|low|close|volume"}
- {"op":"const","value":<number>}
- {"op":"indicator","kind":"ema|sma|rsi","period":<int 2-400>}            // moving averages / RSI on close
- {"op":"indicator","kind":"macd","fast":<int>,"slow":<int>,"signal":<int>,"field":"line|signal|hist"}  // fast<slow
- {"op":"indicator","kind":"atr","period":<int>}                          // average true range (volatility)
- {"op":"indicator","kind":"bollinger","period":<int>,"mult":<0-5>,"field":"upper|mid|lower"}
- {"op":"indicator","kind":"donchian","period":<int>,"field":"upper|lower"} // channel of highs/lows
- {"op":"indicator","kind":"stochastic","period":<int>,"smoothK":<int>,"smoothD":<int>,"field":"k|d"}
- {"op":"indicator","kind":"rollingHigh|rollingLow","period":<int>}       // N-bar high/low

CONDITIONS (true/false at each bar):
- {"op":"gt|lt|gte|lte","left":<operand>,"right":<operand>}
- {"op":"crossAbove|crossBelow","left":<operand>,"right":<operand>}       // left crosses over/under right this bar
- {"op":"and|or","conditions":[<condition>, ...]}                          // 1-8 children
- {"op":"not","condition":<condition>}

RISK (always active alongside the exit rule): {"stopLossPct":<0.1-20>,"takeProfitPct":<0.1-50>, optional "trailingStopPct":<0.1-50>, optional "maxHoldBars":<int>}

RULES:
- "entry" opens a long when it is true and you are flat. "exit" closes the long when true. The risk block (stop-loss / take-profit / optional trailing stop / max-hold) ALWAYS applies on top of the exit rule.
- Pick ANY approach that fits the market and the lessons — trend-following (MA crossovers, Donchian breakouts), mean-reversion (RSI/Stochastic oversold, Bollinger touch), or combinations. Do NOT default to an EMA crossover: it is only one option and is often not the best. Deliberately consider oscillator (RSI/Stochastic) and channel (Bollinger/Donchian) approaches, and favor variety over repeating the same MA-crossover shape.
- Keep it PARSIMONIOUS to avoid overfitting: the count of DISTINCT tunable knobs (each distinct indicator counts by its params; an indicator reused in entry and exit counts once; each distinct const counts 1; plus stopLossPct + takeProfitPct + any optional risk knobs) must be <= the parameter budget stated below.
- Round-trip fees are ~0.15%, so avoid rules that fire constantly on noise; give trades room to develop.
- IMPORTANT window semantics: donchian.upper / rollingHigh INCLUDE the current bar, so "close > donchian.upper" (or rollingHigh) can NEVER be true. For an UPWARD breakout compare the bar's HIGH ({"op":"price","field":"high"}), or use these channels for MEAN-REVERSION (buy near donchian.lower / rollingLow). Likewise close < donchian.lower can't fire — compare the LOW.
- The strategy must actually trade: entries that stack many rare conditions (e.g. RSI<30 AND a deep trend filter AND a band touch) can produce too few trades to be evaluable. Aim for enough entries over the window to clear the minimum-trade requirement while keeping the profit factor healthy.

EXAMPLE 1 — EMA crossover with a trend filter:
{"entry":{"op":"and","conditions":[{"op":"crossAbove","left":{"op":"indicator","kind":"ema","period":20},"right":{"op":"indicator","kind":"ema","period":50}},{"op":"gt","left":{"op":"price","field":"close"},"right":{"op":"indicator","kind":"ema","period":200}}]},"exit":{"op":"crossBelow","left":{"op":"indicator","kind":"ema","period":20},"right":{"op":"indicator","kind":"ema","period":50}},"risk":{"stopLossPct":3,"takeProfitPct":6}}

EXAMPLE 2 — RSI mean-reversion, only in an uptrend:
{"entry":{"op":"and","conditions":[{"op":"lt","left":{"op":"indicator","kind":"rsi","period":14},"right":{"op":"const","value":30}},{"op":"gt","left":{"op":"price","field":"close"},"right":{"op":"indicator","kind":"ema","period":200}}]},"exit":{"op":"gt","left":{"op":"indicator","kind":"rsi","period":14},"right":{"op":"const","value":55}},"risk":{"stopLossPct":2,"takeProfitPct":4}}`;
