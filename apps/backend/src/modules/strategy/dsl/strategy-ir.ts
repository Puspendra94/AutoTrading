/**
 * Strategy IR ("rule tree") — the open-ended, machine-executable representation the LLM emits
 * instead of a fixed EMA-crossover parameter blob. ONE interpreter (interpreter.ts) executes any
 * valid tree for both backtest and live, so new strategy *shapes* need no new engine code — only
 * a genuinely new indicator does. The Python port (worker/strategy/dsl/schema.py) mirrors this,
 * and tools/parity_evaluator.py proves both engines agree.
 *
 * Long-only by design: on Binance spot you can only buy then sell, so a strategy is an `entry`
 * (go long) condition, an `exit` (close long) condition, and a `risk` block (stop/target/etc.).
 */
import { z } from 'zod';

// ---- Operands: a scalar series value at bar i ----
export type PriceField = 'open' | 'high' | 'low' | 'close' | 'volume';

export type Operand =
  | { op: 'const'; value: number }
  | { op: 'price'; field: PriceField }
  | { op: 'indicator'; kind: 'ema' | 'sma' | 'rsi'; period: number; source?: PriceField }
  | { op: 'indicator'; kind: 'macd'; fast: number; slow: number; signal: number; field: 'line' | 'signal' | 'hist'; source?: PriceField }
  | { op: 'indicator'; kind: 'atr'; period: number }
  | { op: 'indicator'; kind: 'bollinger'; period: number; mult: number; field: 'upper' | 'mid' | 'lower'; source?: PriceField }
  | { op: 'indicator'; kind: 'donchian'; period: number; field: 'upper' | 'lower' }
  | { op: 'indicator'; kind: 'stochastic'; period: number; smoothK: number; smoothD: number; field: 'k' | 'd' }
  | { op: 'indicator'; kind: 'rollingHigh' | 'rollingLow'; period: number };

// ---- Conditions: a boolean at bar i ----
export type Condition =
  | { op: 'gt' | 'lt' | 'gte' | 'lte'; left: Operand; right: Operand }
  | { op: 'crossAbove' | 'crossBelow'; left: Operand; right: Operand }
  | { op: 'and' | 'or'; conditions: Condition[] }
  | { op: 'not'; condition: Condition };

export interface RiskBlock {
  stopLossPct: number;
  takeProfitPct: number;
  trailingStopPct?: number; // optional trailing stop from peak-since-entry
  maxHoldBars?: number; // optional time-based exit
}

export interface StrategyIR {
  strategyName: string;
  reasoning: string;
  entry: Condition; // go long when true and flat
  exit: Condition; // close long when true and in a position (in addition to the risk block)
  risk: RiskBlock;
}

// ---- Zod schema (recursive via z.lazy) ----
// Operands are shape-checked loosely here (must be an object with an `op`) and validated
// precisely by validateOperand() below, which produces specific per-field error messages —
// far more readable than a giant discriminated union on (op, kind).
const OperandSchema: z.ZodType<Operand> = z.object({ op: z.string() }).passthrough() as unknown as z.ZodType<Operand>;

const ComparisonOps = ['gt', 'lt', 'gte', 'lte', 'crossAbove', 'crossBelow'] as const;

export const ConditionSchema: z.ZodType<Condition> = z.lazy(() =>
  z.union([
    z.object({
      op: z.enum(ComparisonOps),
      left: OperandSchema,
      right: OperandSchema,
    }),
    z.object({ op: z.enum(['and', 'or']), conditions: z.array(ConditionSchema).min(1).max(8) }),
    z.object({ op: z.literal('not'), condition: ConditionSchema }),
  ]) as unknown as z.ZodType<Condition>,
);

export const StrategyIRSchema = z.object({
  strategyName: z.string().min(1),
  reasoning: z.string().default(''),
  entry: ConditionSchema,
  exit: ConditionSchema,
  risk: z.object({
    stopLossPct: z.number().min(0.1).max(20),
    takeProfitPct: z.number().min(0.1).max(50),
    trailingStopPct: z.number().min(0.1).max(50).optional(),
    maxHoldBars: z.number().int().min(1).max(5000).optional(),
  }),
});

// ---- Semantic validation + limits (beyond the shape checks above) ----
// Bounds mirror the pre-DSL schema's spirit; keep in sync with the Python validator.
const PERIOD_MIN = 2;
const PERIOD_MAX = 400;

export function validateOperand(o: any, path: string): string[] {
  const errs: string[] = [];
  if (o.op === 'const') {
    if (typeof o.value !== 'number' || !Number.isFinite(o.value)) errs.push(`${path}: const.value must be a finite number`);
    return errs;
  }
  if (o.op === 'price') return errs; // field validated by zod enum
  if (o.op !== 'indicator') {
    errs.push(`${path}: unknown operand op '${o.op}'`);
    return errs;
  }
  const period = (p: any) => typeof p === 'number' && Number.isInteger(p) && p >= PERIOD_MIN && p <= PERIOD_MAX;
  switch (o.kind) {
    case 'ema':
    case 'sma':
    case 'rsi':
    case 'atr':
    case 'donchian':
    case 'rollingHigh':
    case 'rollingLow':
      if (!period(o.period)) errs.push(`${path}: ${o.kind}.period must be an int in [${PERIOD_MIN},${PERIOD_MAX}]`);
      if (o.kind === 'donchian' && !['upper', 'lower'].includes(o.field)) errs.push(`${path}: donchian.field must be upper|lower`);
      break;
    case 'macd':
      if (!period(o.fast) || !period(o.slow) || !period(o.signal)) errs.push(`${path}: macd.fast/slow/signal must be ints in range`);
      if (period(o.fast) && period(o.slow) && o.fast >= o.slow) errs.push(`${path}: macd.fast must be < macd.slow`);
      if (!['line', 'signal', 'hist'].includes(o.field)) errs.push(`${path}: macd.field must be line|signal|hist`);
      break;
    case 'bollinger':
      if (!period(o.period)) errs.push(`${path}: bollinger.period out of range`);
      if (typeof o.mult !== 'number' || o.mult <= 0 || o.mult > 5) errs.push(`${path}: bollinger.mult must be in (0,5]`);
      if (!['upper', 'mid', 'lower'].includes(o.field)) errs.push(`${path}: bollinger.field must be upper|mid|lower`);
      break;
    case 'stochastic':
      if (!period(o.period) || !period(o.smoothK) || !period(o.smoothD)) errs.push(`${path}: stochastic.period/smoothK/smoothD out of range`);
      if (!['k', 'd'].includes(o.field)) errs.push(`${path}: stochastic.field must be k|d`);
      break;
    default:
      errs.push(`${path}: unknown indicator kind '${o.kind}'`);
  }
  return errs;
}

function validateCondition(c: any, path: string): string[] {
  const errs: string[] = [];
  if (c.op === 'and' || c.op === 'or') {
    if (!Array.isArray(c.conditions) || c.conditions.length === 0) errs.push(`${path}: ${c.op} needs a non-empty conditions[]`);
    else c.conditions.forEach((sub: any, i: number) => errs.push(...validateCondition(sub, `${path}.${c.op}[${i}]`)));
  } else if (c.op === 'not') {
    errs.push(...validateCondition(c.condition, `${path}.not`));
  } else if ((ComparisonOps as readonly string[]).includes(c.op)) {
    errs.push(...validateOperand(c.left, `${path}.left`), ...validateOperand(c.right, `${path}.right`));
  } else {
    errs.push(`${path}: unknown condition op '${c.op}'`);
  }
  return errs;
}

/** Full semantic validation. Returns [] when the tree is executable, else a list of reasons. */
export function validateIR(ir: StrategyIR): string[] {
  const parsed = StrategyIRSchema.safeParse(ir);
  if (!parsed.success) return parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
  return [...validateCondition(ir.entry, 'entry'), ...validateCondition(ir.exit, 'exit')];
}

// ---- Warmup: bars needed before any indicator in the tree is defined ----
function operandLookback(o: Operand): number {
  if (o.op !== 'indicator') return 0;
  switch (o.kind) {
    case 'ema':
    case 'sma':
    case 'bollinger':
    case 'donchian':
    case 'rollingHigh':
    case 'rollingLow':
      return (o as any).period;
    case 'rsi':
    case 'atr':
      return (o as any).period + 1;
    case 'macd':
      return (o as any).slow + (o as any).signal;
    case 'stochastic':
      return (o as any).period + (o as any).smoothK + (o as any).smoothD;
    default:
      return 0;
  }
}

function conditionLookback(c: Condition): number {
  if (c.op === 'and' || c.op === 'or') return Math.max(0, ...c.conditions.map(conditionLookback));
  if (c.op === 'not') return conditionLookback(c.condition);
  const cmp = c as { left: Operand; right: Operand };
  return Math.max(operandLookback(cmp.left), operandLookback(cmp.right));
}

/** First bar index at which the tree may fire. max(1, ...) guarantees cross-ops always have a
 * valid i-1; for a plain EMA crossover this equals the slow-EMA period, matching the pre-DSL
 * engine's loop start exactly (so an auto-translated legacy strategy reproduces it bar-for-bar). */
export function warmupBars(ir: StrategyIR): number {
  return Math.max(1, conditionLookback(ir.entry), conditionLookback(ir.exit));
}

// ---- Overfitting budget: count DISTINCT tunable numeric knobs across the whole tree ----
// Reusing the same indicator in both entry and exit is NOT extra freedom, so operands are
// deduped by their tuned params (ignoring `field` — macd line/signal/hist share fast/slow/signal,
// bollinger upper/mid/lower share period/mult, etc.). This keeps the count aligned with the
// pre-DSL "number of indicatorConfig keys" measure: a plain EMA crossover = 2 EMAs + SL + TP = 4.
function operandParamCount(o: Operand): number {
  if (o.op === 'const') return 1;
  if (o.op === 'price') return 0;
  switch (o.kind) {
    case 'macd':
      return 3; // fast + slow + signal
    case 'bollinger':
      return 2; // period + mult
    case 'stochastic':
      return 3; // period + smoothK + smoothD
    default:
      return 1; // period
  }
}
/** Dedupe key that ignores the multi-output `field` selector so shared indicators count once. */
function operandKey(o: Operand): string {
  const { field, ...rest } = o as any;
  return JSON.stringify(rest);
}
function collectOperands(c: Condition, into: Map<string, Operand>): void {
  if (c.op === 'and' || c.op === 'or') {
    c.conditions.forEach((sub) => collectOperands(sub, into));
    return;
  }
  if (c.op === 'not') {
    collectOperands(c.condition, into);
    return;
  }
  const cmp = c as { left: Operand; right: Operand };
  for (const o of [cmp.left, cmp.right]) if (o.op !== 'price') into.set(operandKey(o), o);
}
export function countIRParameters(ir: StrategyIR): number {
  const operands = new Map<string, Operand>();
  collectOperands(ir.entry, operands);
  collectOperands(ir.exit, operands);
  let sum = 0;
  for (const o of operands.values()) sum += operandParamCount(o);
  const risk = 2 + (ir.risk.trailingStopPct != null ? 1 : 0) + (ir.risk.maxHoldBars != null ? 1 : 0);
  return sum + risk;
}

/**
 * Resolve any strategy params blob to a validated IR: a legacy indicatorConfig blob is
 * auto-translated; a rule-tree blob is validated as-is. Returns null when the result isn't a
 * valid, executable tree (caller should then treat the strategy as producing no trades).
 */
export function resolveIR(params: any): StrategyIR | null {
  const ir = isLegacyParams(params) ? legacyToIR(params) : (params as StrategyIR);
  return validateIR(ir).length === 0 ? ir : null;
}

/**
 * Coarse tag grouping similar strategies for lesson retrieval — the sorted distinct indicator
 * kinds used across the tree (e.g. 'ema', 'ema_rsi', 'bollinger_ema', 'macd'). Legacy params are
 * auto-translated first. 'unclassified' if the params aren't a valid tree; 'price_action' if a
 * valid tree uses no indicators (pure price/level rules).
 */
export function strategyTypeTag(params: any): string {
  // Lenient by design: tagging only reads the tree structure, so it must not require a valid
  // strategyName/risk (unlike resolveIR). Legacy params are auto-translated first.
  const ir: any = isLegacyParams(params) ? legacyToIR(params) : params;
  if (!ir || !ir.entry || !ir.exit) return 'unclassified';
  const ops = new Map<string, Operand>();
  try {
    collectOperands(ir.entry, ops);
    collectOperands(ir.exit, ops);
  } catch {
    return 'unclassified';
  }
  const kinds = new Set<string>();
  for (const o of ops.values()) if (o.op === 'indicator') kinds.add(o.kind);
  return kinds.size ? [...kinds].sort().join('_') : 'price_action';
}

/** One-line human description of a rule tree for log/fallback text (IR or legacy params). */
export function describeStrategy(params: any): string {
  const ir = resolveIR(params);
  if (!ir) return 'invalid/unrecognized strategy params';
  const risk = `SL ${ir.risk.stopLossPct}% / TP ${ir.risk.takeProfitPct}%` +
    (ir.risk.trailingStopPct != null ? ` / trail ${ir.risk.trailingStopPct}%` : '') +
    (ir.risk.maxHoldBars != null ? ` / maxHold ${ir.risk.maxHoldBars}` : '');
  return `${strategyTypeTag(params)} strategy (${countIRParameters(ir)} knobs, ${risk})`;
}

// ---- Back-compat: translate a legacy EMA-crossover params blob into an equivalent IR ----
// Mirrors the pre-DSL logic exactly: entry = fast crosses ABOVE slow (+ optional close>trendEMA);
// exit = fast BELOW slow (a level check, not a cross — same as the old engine); SL/TP in risk.
export function legacyToIR(params: any): StrategyIR {
  const cfg = params?.indicatorConfig || {};
  const fast = cfg.emaFastPeriod || 12;
  const slow = cfg.emaSlowPeriod || 26;
  const trend = cfg.trendEmaPeriod;
  const entryConds: Condition[] = [
    { op: 'crossAbove', left: { op: 'indicator', kind: 'ema', period: fast }, right: { op: 'indicator', kind: 'ema', period: slow } },
  ];
  if (trend) {
    entryConds.push({ op: 'gt', left: { op: 'price', field: 'close' }, right: { op: 'indicator', kind: 'ema', period: trend } });
  }
  return {
    strategyName: params?.strategyName || 'Legacy EMA Crossover',
    reasoning: 'Auto-translated from legacy indicatorConfig params.',
    entry: entryConds.length === 1 ? entryConds[0] : { op: 'and', conditions: entryConds },
    exit: { op: 'lt', left: { op: 'indicator', kind: 'ema', period: fast }, right: { op: 'indicator', kind: 'ema', period: slow } },
    risk: { stopLossPct: cfg.stopLossPct || 1.5, takeProfitPct: cfg.takeProfitPct || 3.5 },
  };
}

/** True when a params blob is legacy (has indicatorConfig) rather than a rule-tree IR. */
export function isLegacyParams(params: any): boolean {
  return !!params?.indicatorConfig && !params?.entry;
}
