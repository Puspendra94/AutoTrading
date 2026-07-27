import { z } from 'zod';

/**
 * The meta-planner's output (spec: adaptive generation loop). Before generating a strategy, the
 * AI reads ALL accumulated memory — failed-generation lessons AND live-performance insights on
 * the active strategy — and decides HOW to build the next one: which timeframe, how much data,
 * which signals to lean on, and (within HARD SAFE FLOORS it can never breach) how strict the
 * evaluation gate should be.
 *
 * The proposed gate thresholds here are only a REQUEST — StrategyEngineService clamps every one
 * to a hard floor so the generator can never weaken its own test into meaninglessness (a race to
 * the bottom). The clamped, effective plan is what's logged and used.
 */
export const GenerationPlanSchema = z.object({
  interval: z
    .enum(['1h', '2h', '4h', '6h', '12h', '1d'])
    .describe(
      'Timeframe to generate & backtest on. 1m/short intervals are noise/fee-dominated for trend ' +
        'following (no edge); higher intervals trade fewer, cleaner swings. Pick from the accumulated ' +
        'lessons (e.g. if past attempts had too few trades, go shorter; if whipsawy, go longer).',
    ),
  candleLimit: z
    .number()
    .int()
    .min(1000)
    .max(20000)
    .describe(
      'How many bars of `interval` to backtest over. More bars = more out-of-sample trades / a more ' +
        'reliable Sharpe, but reaches into older regimes where the edge may not hold. ~10000 1h bars ≈ 14 months.',
    ),
  minSharpe: z
    .number()
    .min(0)
    .max(3)
    .describe('Proposed out-of-sample Sharpe bar. Clamped to a hard floor of 0.5 before use.'),
  minProfitFactor: z
    .number()
    .min(1)
    .max(3)
    .describe('Proposed profit-factor bar. Clamped to a hard floor of 1.2 before use.'),
  maxDrawdownPct: z
    .number()
    .min(5)
    .max(50)
    .describe('Proposed max-drawdown ceiling (percent). Clamped to a hard ceiling of 25 before use.'),
  minTradeCount: z
    .number()
    .int()
    .min(5)
    .max(300)
    .describe('Proposed minimum out-of-sample trades. Clamped to a hard floor of 5 before use.'),
  signalEmphasis: z
    .string()
    .describe(
      'Short, concrete guidance injected into the strategy-generation prompt — which parameters/signals ' +
        'to favor given what the lessons show (e.g. "use the trendEmaPeriod filter with faster EMAs 9/21 to ' +
        'lift trade count while keeping profit factor up").',
    ),
  reasoning: z.string().describe('Brief rationale tying the plan to the lessons it was drawn from'),
});

export type GenerationPlan = z.infer<typeof GenerationPlanSchema>;
