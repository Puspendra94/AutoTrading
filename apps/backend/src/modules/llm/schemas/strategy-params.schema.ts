import { z } from 'zod';

/**
 * Canonical shape every LLM-proposed strategy must be forced into, regardless of
 * which provider in LLM_MODELS answers — this is what strategy-evaluator.service.ts
 * and ai-lessons.service.ts actually read (indicatorConfig.emaFastPeriod/emaSlowPeriod/
 * stopLossPct/takeProfitPct). Passed to LangChain's withStructuredOutput() so the model's
 * own tool-calling/JSON-mode is used to guarantee the shape, instead of hoping a prose
 * prompt is followed.
 *
 * Only the four knobs the backtest and live engines actually trade on are proposable. An
 * RSI filter used to live here but was never simulated or executed anywhere — it only
 * inflated the parameter count (the overfitting-budget gate), so it's been removed. Any real
 * momentum filter added later must be wired into the simulate/live-signal paths of BOTH
 * engines (and re-verified by the parity harness) before being reintroduced here.
 */
export const StrategyParamsSchema = z.object({
  strategyName: z.string().describe('Short human-readable name for this strategy variant'),
  indicatorConfig: z.object({
    emaFastPeriod: z.number().int().min(2).max(100).describe('Fast EMA period in candles'),
    emaSlowPeriod: z.number().int().min(5).max(300).describe('Slow EMA period in candles'),
    trendEmaPeriod: z
      .number()
      .int()
      .min(20)
      .max(400)
      .optional()
      .describe(
        'Long-term trend EMA period; LONG entries are only taken when price is above this EMA ' +
          '(trend-regime filter that cuts whipsaws in ranging/down markets). Should be longer than ' +
          'emaSlowPeriod. Omit to disable the filter.',
      ),
    stopLossPct: z.number().min(0.1).max(20).describe('Stop-loss as a percent, e.g. 1.5 for 1.5%'),
    takeProfitPct: z.number().min(0.1).max(50).describe('Take-profit as a percent, e.g. 3.5 for 3.5%'),
  }),
  reasoning: z.string().describe('Brief rationale for these parameter choices'),
});

export type StrategyParams = z.infer<typeof StrategyParamsSchema>;
