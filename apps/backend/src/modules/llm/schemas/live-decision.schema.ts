import { z } from 'zod';

/** Mode B (spec 2.6/4.4) live-decision output — forced via withStructuredOutput so
 * every provider in LLM_MODELS returns an actionable decision, never free-form prose. */
export const LiveDecisionSchema = z.object({
  action: z.enum(['BUY', 'SELL', 'HOLD']).describe('The trading action to take right now'),
  reasoning: z.string().describe('Brief rationale for this decision'),
});

export type LiveDecision = z.infer<typeof LiveDecisionSchema>;

/** Phase 2 hybrid AI exit overlay output. The AI may only EXIT early to protect profit or HOLD to
 * keep riding — it can never re-open a position or block a deterministic rules exit. */
export const ExitTightenDecisionSchema = z.object({
  action: z
    .enum(['EXIT', 'HOLD'])
    .describe('EXIT to book the open position now (the move looks exhausted), or HOLD to keep riding the trend'),
  reasoning: z.string().describe('Brief rationale for this decision'),
});

export type ExitTightenDecision = z.infer<typeof ExitTightenDecisionSchema>;
