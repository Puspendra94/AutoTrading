import { z } from 'zod';

/**
 * Open-ended strategy generation output (DSL Phase 4/5). The LLM composes an arbitrary rule tree
 * from the indicator grammar rather than a fixed EMA-crossover parameter blob.
 *
 * `entry`/`exit`/`risk` are typed permissively (z.any) ON PURPOSE: the tree is recursive
 * (and/or/not nesting) and our provider chain includes DeepSeek jsonMode (sends no schema) and
 * Bedrock models — a deep nested response schema isn't reliably honored, and models naturally
 * emit the tree as nested objects (not a stringified field). A permissive schema lets every
 * provider's JSON coerce cleanly; the REAL structural check is validateIR() in the caller, which
 * rejects+retries anything malformed. The full grammar is spelled out in the generation prompt.
 */
export const StrategyGenSchema = z.object({
  strategyName: z.string().describe('Short human-readable name for this strategy'),
  reasoning: z.string().optional().describe('Brief rationale, tied to the lessons and the market'),
  entry: z.any().describe('Condition tree that, when true and flat, opens a long position'),
  exit: z.any().describe('Condition tree that closes the open long (the risk block also applies)'),
  risk: z.any().describe('{stopLossPct, takeProfitPct, and optionally trailingStopPct, maxHoldBars}'),
});

export type StrategyGen = z.infer<typeof StrategyGenSchema>;
