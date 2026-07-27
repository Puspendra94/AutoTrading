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
  holdBars: number[]; // bars held per closed trade (parallel to `trades`), for the avg-hold gate
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

// ---- Direction-aware fills / P&L / extreme (Phase 4). SHORT sells to open, buys to close. ----
const entryFill = (dir: string, price: number) => price * (dir === 'short' ? 0.9995 : 1.0005);
const exitFill = (dir: string, price: number) => price * (dir === 'short' ? 1.0005 : 0.9995);
const netReturn = (dir: string, entry: number, exit: number) =>
  (dir === 'short' ? (entry - exit) / entry : (exit - entry) / entry) - 0.0015; // 0.15% round-trip fee
/** Track the favorable extreme: trough (min) for a short, peak (max) for a long. */
const betterExtreme = (dir: string, extreme: number, price: number) =>
  dir === 'short' ? (price < extreme ? price : extreme) : price > extreme ? price : extreme;

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
  ctx: { entryPrice: number; barsHeld: number; extremePrice: number },
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
  const holdBars: number[] = []; // bars held per closed trade (parallel to `trades`), for the avg-hold gate
  const s = buildSeries(candles);
  const cache: Cache = new Map();
  const warmup = warmupBars(ir);

  const direction = ir.direction ?? 'long';
  let position: 'NONE' | 'OPEN' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let extremePrice = 0;
  let equity = 10000;
  let peakEquity = equity;
  let maxDrawdown = 0;
  let currentDrawdownDuration = 0;
  let maxDrawdownDuration = 0;

  if (candles.length <= warmup) return { trades, holdBars, maxDrawdown: 0, drawdownDuration: 0 };

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
        position = 'OPEN';
        entryPrice = entryFill(direction, price); // 0.05% entry slippage (buy for long, sell for short)
        barsHeld = 0;
        extremePrice = price;
      }
    } else {
      barsHeld++;
      extremePrice = betterExtreme(direction, extremePrice, price);
      const reason = exitDecision(ir, s, cache, i, { entryPrice, barsHeld, extremePrice });
      if (reason) {
        const exitP = exitFill(direction, price); // 0.05% exit slippage
        const netReturnPct = netReturn(direction, entryPrice, exitP); // incl. 0.15% round-trip fee
        equity += equity * netReturnPct;
        trades.push(netReturnPct);
        holdBars.push(barsHeld);
        position = 'NONE';
      }
    }
  }

  return { trades, holdBars, maxDrawdown, drawdownDuration: maxDrawdownDuration };
}

/** Exit decision against a prepared Series/cache (internal fast path used by the sim loop). */
function exitDecision(
  ir: StrategyIR,
  s: ind.Series,
  cache: Cache,
  i: number,
  ctx: { entryPrice: number; barsHeld: number; extremePrice: number },
): string | null {
  // Adaptive exit ladder (Phase 1) — direction-aware (Phase 4). Deterministic and fully
  // backtestable; a live AI overlay (Phase 2) may only TIGHTEN this. `extremePrice` is the
  // peak-since-entry for a long, the trough-since-entry for a short. Mirrors _exit_decision in
  // interpreter.py.
  const price = s.close[i];
  const { entryPrice, barsHeld, extremePrice } = ctx;
  const isShort = ir.direction === 'short';
  // Short profits when price FALLS; the favorable extreme is the trough and the trailing stop is
  // triggered by an adverse RISE off it.
  const returnPct = isShort ? (entryPrice - price) / entryPrice : (price - entryPrice) / entryPrice;
  const extremeReturn =
    extremePrice > 0
      ? isShort
        ? (entryPrice - extremePrice) / entryPrice
        : (extremePrice - entryPrice) / entryPrice
      : returnPct;
  const adverseFromExtreme =
    extremePrice > 0 ? (isShort ? (price - extremePrice) / extremePrice : (extremePrice - price) / extremePrice) : 0;
  const risk = ir.risk;

  // 1. Hard stop-loss — absolute safety backstop, never gated.
  if (returnPct <= -risk.stopLossPct / 100) return 'Stop loss';

  // 2. Profit-protection floor — active whenever a trigger is configured (resolveIR injects the
  //    system default so every real strategy has it). Once peak gain reaches the trigger, the trade
  //    may never round-trip below the floor. This is what stops an 8% gain becoming a 2% loss.
  //    Absent trigger => off (preserves pre-DSL parity for raw IRs that never went through resolve).
  if (risk.breakevenTriggerPct != null) {
    const beFloor = risk.breakevenFloorPct ?? 0;
    if (extremeReturn >= risk.breakevenTriggerPct / 100 && returnPct <= beFloor / 100) return 'Profit floor';
  }

  // 3. Take-profit target. hard = book immediately; soft = ride past it under a tight trail (#4).
  //    Absent mode => 'hard' (legacy behavior for raw IRs).
  const tpMode = risk.takeProfitMode ?? 'hard';
  if (returnPct >= risk.takeProfitPct / 100 && tpMode === 'hard') return 'Take profit';

  // 4. Trailing stop off the favorable extreme. The strategy's own trail (if any) is always active;
  //    in soft-TP mode a tighter post-target trail kicks in once the extreme has reached the target.
  let effTrail = risk.trailingStopPct ?? null;
  if (tpMode === 'soft' && extremeReturn >= risk.takeProfitPct / 100) {
    const postTrail = risk.postTargetTrailPct ?? 2.0;
    effTrail = effTrail == null ? postTrail : Math.min(effTrail, postTrail);
  }
  if (effTrail != null && extremePrice > 0) {
    if (adverseFromExtreme >= effTrail / 100) return 'Trailing stop';
  }

  // 5. Time-based max hold.
  if (risk.maxHoldBars != null && barsHeld >= risk.maxHoldBars) return 'Max hold';

  // 6. Rule-based exit — gated by a minimum hold so a noisy signal can't open and slam shut on the
  //    same/adjacent bar (the whipsaw that produced 0.00% round-trips live). Absent => 0 (no gate).
  const minHold = risk.minHoldBars ?? 0;
  if (barsHeld >= minHold && evalCondition(ir.exit, s, cache, i)) return 'Exit rule';
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

  const direction = ir.direction ?? 'long';
  const openSide = direction === 'short' ? 'sell' : 'buy'; // short opens by selling
  const closeSide = direction === 'short' ? 'buy' : 'sell'; // ...and closes by buying back
  let position: 'NONE' | 'OPEN' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let extremePrice = 0;
  for (let i = warmup; i < candles.length; i++) {
    const price = s.close[i];
    if (!Number.isFinite(price) || !Number.isFinite(times[i])) continue;
    if (position === 'NONE') {
      if (evalCondition(ir.entry, s, cache, i)) {
        position = 'OPEN';
        entryPrice = price;
        barsHeld = 0;
        extremePrice = price;
        signals.push({ time: times[i], side: openSide, price, reason: 'Entry rule' });
      }
    } else {
      barsHeld++;
      extremePrice = betterExtreme(direction, extremePrice, price);
      const reason = exitDecision(ir, s, cache, i, { entryPrice, barsHeld, extremePrice });
      if (reason) {
        position = 'NONE';
        signals.push({ time: times[i], side: closeSide, price, reason });
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
export function intendedPositionState(ir: StrategyIR, candles: Candle[]): 'NONE' | 'LONG' | 'SHORT' {
  const s = buildSeries(candles);
  const cache: Cache = new Map();
  const warmup = warmupBars(ir);
  if (candles.length <= warmup) return 'NONE';

  const direction = ir.direction ?? 'long';
  const holdingState: 'LONG' | 'SHORT' = direction === 'short' ? 'SHORT' : 'LONG';
  let position: 'NONE' | 'LONG' | 'SHORT' = 'NONE';
  let entryPrice = 0;
  let barsHeld = 0;
  let extremePrice = 0;
  for (let i = warmup; i < candles.length; i++) {
    const price = s.close[i];
    if (!Number.isFinite(price)) continue;
    if (position === 'NONE') {
      if (evalCondition(ir.entry, s, cache, i)) {
        position = holdingState;
        entryPrice = price;
        barsHeld = 0;
        extremePrice = price;
      }
    } else {
      barsHeld++;
      extremePrice = betterExtreme(direction, extremePrice, price);
      if (exitDecision(ir, s, cache, i, { entryPrice, barsHeld, extremePrice })) {
        position = 'NONE';
      }
    }
  }
  return position;
}
