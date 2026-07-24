import { z } from 'zod';

/**
 * Canonical shape every LLM-proposed strategy must be forced into, regardless of
 * which provider in LLM_MODELS answers — this is what strategy-evaluator.service.ts
 * and ai-lessons.service.ts actually read (indicatorConfig.emaFastPeriod/emaSlowPeriod/
 * stopLossPct/takeProfitPct, and rsiPeriod for strategy-type tagging). Passed to
 * LangChain's withStructuredOutput() so the model's own tool-calling/JSON-mode is used
 * to guarantee the shape, instead of hoping a prose prompt is followed.
 */
export const StrategyParamsSchema = z.object({
  strategyName: z.string().describe('Short human-readable name for this strategy variant'),
  indicatorConfig: z.object({
    emaFastPeriod: z.number().int().min(2).max(100).describe('Fast EMA period in candles'),
    emaSlowPeriod: z.number().int().min(5).max(300).describe('Slow EMA period in candles'),
    rsiPeriod: z.number().int().min(2).max(50).optional().describe('RSI lookback period, if the strategy uses RSI'),
    rsiBuyThreshold: z.number().min(0).max(100).optional(),
    rsiSellThreshold: z.number().min(0).max(100).optional(),
    stopLossPct: z.number().min(0.1).max(20).describe('Stop-loss as a percent, e.g. 1.5 for 1.5%'),
    takeProfitPct: z.number().min(0.1).max(50).describe('Take-profit as a percent, e.g. 3.5 for 3.5%'),
  }),
  reasoning: z.string().describe('Brief rationale for these parameter choices'),
});

export type StrategyParams = z.infer<typeof StrategyParamsSchema>;
