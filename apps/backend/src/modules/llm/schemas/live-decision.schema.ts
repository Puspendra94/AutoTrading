import { z } from 'zod';

/** Mode B (spec 2.6/4.4) live-decision output — forced via withStructuredOutput so
 * every provider in LLM_MODELS returns an actionable decision, never free-form prose. */
export const LiveDecisionSchema = z.object({
  action: z.enum(['BUY', 'SELL', 'HOLD']).describe('The trading action to take right now'),
  reasoning: z.string().describe('Brief rationale for this decision'),
});

export type LiveDecision = z.infer<typeof LiveDecisionSchema>;
