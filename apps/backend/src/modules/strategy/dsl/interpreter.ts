/**
 * The one interpreter that executes any StrategyIR rule tree — used by BOTH the backtester and
 * the live-signal path so what is promoted is exactly what trades. It resolves each operand to a
 * pre-computed number[] (memoized), evaluates conditions bar-by-bar, and runs the SAME
 * fill/slippage/fee/equity accounting the pre-DSL engine used (0.05% slippage per side, 0.15%
 * round-trip fee) so metrics stay comparable. The Python port (worker/strategy/dsl/interpreter.py)
 * mirrors this and is diffed by tools/parity_evaluator.py.
 */
import * as ind from './indicators';
import { Condition, Operand, StrategyIR, warmupBars } from './strategy-ir';

export interface Candle {
  open?: number | string;
  high?: number | string;
  low?: number | string;
  close: number | string;
  volume?: number | string;
  timestamp?: any;
}

export interface TradeSimResult {
  trades: number[];
  maxDrawdown: number;
  drawdownDuration: number;
}

export interface IRSignal {
  time: number;
  side: 'buy' | 'sell';
  price: number;
  reason: string;
}

function buildSeries(candles: Candle[]): ind.Series {
  const num = (v: any) => Number(v);
  return {
    open: candles.map((c) => num(c.open ?? c.close)),
    high: candles.map((c) => num(c.high ?? c.close)),
    low: candles.map((c) => num(c.low ?? c.close)),
    close: candles.map((c) => num(c.close)),
    volume: candles.map((c) => num(c.volume ?? 0)),
  };
}

type Cache = Map<string, number[]>;

function seriesForOperand(o: Operand, s: ind.Series, cache: Cache): number[] {
  if (o.op === 'const') return new Array(s.close.length).fill(o.value);
  if (o.op === 'price') return s[o.field];
  const key = JSON.stringify(o);
  const cached = cache.get(key);
  if (cached) return cached;
  const src = (kind: any): number[] => s[(o as any).source || 'close'];
  let arr: number[];
  switch (o.kind) {
    case 'ema': arr = ind.ema(src(o), o.period); break;
    case 'sma': arr = ind.sma(src(o), o.period); break;
    case 'rsi': arr = ind.rsi(src(o), o.period); break;
    case 'atr': arr = ind.atr(s, o.period); break;
    case 'macd': {
      const m = ind.macd(src(o), o.fast, o.slow, o.signal);
      arr = o.field === 'signal' ? m.signal : o.field === 'hist' ? m.hist : m.line;
      break;
    }
    case 'bollinger': {
      const b = ind.bollinger(src(o), o.period, o.mult);
      arr = o.field === 'upper' ? b.upper : o.field === 'lower' ? b.lower : b.mid;
      break;
    }
    case 'donchian': {
      const d = ind.donchian(s, o.period);
      arr = o.field === 'upper' ? d.upper : d.lower;
      break;
    }
    case 'stochastic': {
      const st = ind.stochastic(s, o.period, o.smoothK, o.smoothD);
      arr = o.field === 'd' ? st.d : st.k;
      break;
    }
    case 'rollingHigh': arr = ind.rollingMax(s.high, o.period); break;
    case 'rollingLow': arr = ind.rollingMin(s.low, o.period); break;
    default: arr = new Array(s.close.length).fill(NaN);
  }
  cache.set(key, arr);
  return arr;
}

const fin = (x: number) => Number.isFinite(x);

function evalCondition(c: Condition, s: ind.Series, cache: Cache, i: number): boolean {
  switch (c.op) {
    case 'and': return c.conditions.every((sub) => evalCondition(sub, s, cache, i));
    case 'or': return c.conditions.some((sub) => evalCondition(sub, s, cache, i));
    case 'not': return !evalCondition(c.condition, s, cache, i);
  }
  const L = seriesForOperand(c.left, s, cache);
  const R = seriesForOperand(c.right, s, cache);
  const a = L[i];
  const b = R[i];
  switch (c.op) {
    case 'gt': return fin(a) && fin(b) && a > b;
    case 'lt': return fin(a) && fin(b) && a < b;
    case 'gte': return fin(a) && fin(b) && a >= b;
    case 'lte': return fin(a) && fin(b) && a <= b;
    case 'crossAbove': {
      const ap = L[i - 1];
      const bp = R[i - 1];
      return fin(a) && fin(b) && fin(ap) && fin(bp) && a > b && ap <= bp;
    }
    case 'crossBelow': {
      const ap = L[i - 1];
      const bp = R[i - 1];
      return fin(a) && fin(b) && fin(ap) && fin(bp) && a < b && ap >= bp;
    }
    default: return false;
  }
}

/** Should we go long right now (flat)? Public so the live-signal path can reuse it. */
export function shouldEnter(ir: StrategyIR, candles: Candle[], i: number): boolean {
  const s = buildSeries(candles);
  return i >= warmupBars(ir) && evalCondition(ir.entry, s, new Map(), i);
}

/**
 * Should we exit the open long at bar i? Combines the rule-based `exit` condition with the risk
 * block (stop-loss / take-profit / trailing / max-hold), evaluated off the given entry context.
 * Returns the exit reason or null. `entryPrice` is the fill price (post-slippage) of the position.
 */
export function exitReason(
  ir: StrategyIR,
  candles: Candle[],
  i: number,
  ctx: { entryPrice: number; barsHeld: number; peakPrice: number },
): string | null {
  return exitDecision(ir, buildSeries(candles), new Map(), i, ctx);
}

/**
 * Backtest the IR over candles — identical accounting to the pre-DSL simulateTrades so metrics
 * are apples-to-apples: 0.05% slippage each side, 0.15% round-trip fee, equity compounding,
 * max drawdown + drawdown duration in bars. Returns the per-trade net-return series the metric
 * layer already knows how to score.
 */
export function simulateFromIR(candles: Candle[], ir: StrategyIR): TradeSimResult {
  const trades: number[] = [];
  const s = buildSeries(candles);
  const cache: Cache = new Map();
  const warmup = warmupBars(ir);

  let position: 'NONE' | 'LONG' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let peakPrice = 0;
  let equity = 10000;
  let peakEquity = equity;
  let maxDrawdown = 0;
  let currentDrawdownDuration = 0;
  let maxDrawdownDuration = 0;

  if (candles.length <= warmup) return { trades, maxDrawdown: 0, drawdownDuration: 0 };

  for (let i = warmup; i < candles.length; i++) {
    const price = s.close[i];

    if (equity > peakEquity) {
      peakEquity = equity;
      currentDrawdownDuration = 0;
    } else {
      currentDrawdownDuration++;
      const dd = ((peakEquity - equity) / peakEquity) * 100;
      if (dd > maxDrawdown) maxDrawdown = dd;
      if (currentDrawdownDuration > maxDrawdownDuration) maxDrawdownDuration = currentDrawdownDuration;
    }

    if (position === 'NONE') {
      if (evalCondition(ir.entry, s, cache, i)) {
        position = 'LONG';
        entryPrice = price * 1.0005; // 0.05% entry slippage
        barsHeld = 0;
        peakPrice = price;
      }
    } else {
      barsHeld++;
      if (price > peakPrice) peakPrice = price;
      const reason = exitDecision(ir, s, cache, i, { entryPrice, barsHeld, peakPrice });
      if (reason) {
        const exitPrice = price * 0.9995; // 0.05% exit slippage
        const netReturnPct = (exitPrice - entryPrice) / entryPrice - 0.0015; // 0.15% round-trip fee
        equity += equity * netReturnPct;
        trades.push(netReturnPct);
        position = 'NONE';
      }
    }
  }

  return { trades, maxDrawdown, drawdownDuration: maxDrawdownDuration };
}

/** Exit decision against a prepared Series/cache (internal fast path used by the sim loop). */
function exitDecision(
  ir: StrategyIR,
  s: ind.Series,
  cache: Cache,
  i: number,
  ctx: { entryPrice: number; barsHeld: number; peakPrice: number },
): string | null {
  const price = s.close[i];
  const returnPct = (price - ctx.entryPrice) / ctx.entryPrice;
  if (returnPct <= -ir.risk.stopLossPct / 100) return 'Stop loss';
  if (returnPct >= ir.risk.takeProfitPct / 100) return 'Take profit';
  if (ir.risk.trailingStopPct != null && ctx.peakPrice > 0) {
    const drop = (ctx.peakPrice - price) / ctx.peakPrice;
    if (drop >= ir.risk.trailingStopPct / 100) return 'Trailing stop';
  }
  if (ir.risk.maxHoldBars != null && ctx.barsHeld >= ir.risk.maxHoldBars) return 'Max hold';
  if (evalCondition(ir.exit, s, cache, i)) return 'Exit rule';
  return null;
}

/** Timestamped buy/sell markers for the chart — same decisions as simulateFromIR, surfaced as
 * intents rather than a return series. */
export function generateSignalsFromIR(candles: Candle[], ir: StrategyIR): IRSignal[] {
  const signals: IRSignal[] = [];
  const s = buildSeries(candles);
  const cache: Cache = new Map();
  const warmup = warmupBars(ir);
  const times = candles.map((c) => Math.floor(new Date(c.timestamp).getTime() / 1000));

  let position: 'NONE' | 'LONG' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let peakPrice = 0;
  for (let i = warmup; i < candles.length; i++) {
    const price = s.close[i];
    if (!Number.isFinite(price) || !Number.isFinite(times[i])) continue;
    if (position === 'NONE') {
      if (evalCondition(ir.entry, s, cache, i)) {
        position = 'LONG';
        entryPrice = price;
        barsHeld = 0;
        peakPrice = price;
        signals.push({ time: times[i], side: 'buy', price, reason: 'Entry rule' });
      }
    } else {
      barsHeld++;
      if (price > peakPrice) peakPrice = price;
      const reason = exitDecision(ir, s, cache, i, { entryPrice, barsHeld, peakPrice });
      if (reason) {
        position = 'NONE';
        signals.push({ time: times[i], side: 'sell', price, reason });
      }
    }
  }
  return signals;
}

/**
 * The strategy's CURRENT intended position at the most recent bar, derived from the exact same
 * stateful replay the backtest and chart intents use (entry edge in, rule/risk exit out). The
 * live loop uses this to reconcile its real exposure to the strategy's intended exposure rather
 * than only reacting to a fresh entry edge on the current bar: a strategy that is activated — or
 * whose backend restarted — while already mid-trade then opens the position to MATCH the strategy
 * (instead of sitting flat until the next crossover, which looks like "it never trades"). In
 * steady state this is identical to the edge check (the bar the strategy enters is the bar it
 * becomes intended-LONG); it only differs by self-healing when the live position has fallen out
 * of sync with the strategy's own signals. Returns 'LONG' when the strategy would currently be
 * holding, else 'NONE'.
 */
export function intendedPositionState(ir: StrategyIR, candles: Candle[]): 'NONE' | 'LONG' {
  const s = buildSeries(candles);
  const cache: Cache = new Map();
  const warmup = warmupBars(ir);
  if (candles.length <= warmup) return 'NONE';

  let position: 'NONE' | 'LONG' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let peakPrice = 0;
  for (let i = warmup; i < candles.length; i++) {
    const price = s.close[i];
    if (!Number.isFinite(price)) continue;
    if (position === 'NONE') {
      if (evalCondition(ir.entry, s, cache, i)) {
        position = 'LONG';
        entryPrice = price;
        barsHeld = 0;
        peakPrice = price;
      }
    } else {
      barsHeld++;
      if (price > peakPrice) peakPrice = price;
      if (exitDecision(ir, s, cache, i, { entryPrice, barsHeld, peakPrice })) {
        position = 'NONE';
      }
    }
  }
  return position;
}
