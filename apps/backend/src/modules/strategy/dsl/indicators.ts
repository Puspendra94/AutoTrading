/**
 * Deterministic technical-indicator library for the strategy DSL (Phase 1).
 *
 * Every function returns an array aligned 1:1 with the input candles; positions before an
 * indicator has enough history to be defined hold `NaN` (the interpreter treats any condition
 * touching a NaN as false, and never enters before the tree's warmup bar). These formulas are
 * the CANONICAL definitions — the Python port in worker/strategy/dsl/indicators.py must match
 * them exactly, and tools/parity_evaluator.py asserts it. Keep them simple and explicit for
 * that reason (no clever vectorization that could drift between languages).
 *
 * Conventions chosen for cross-language reproducibility:
 *   - EMA seeds ema[0] = price[0] (same as the pre-DSL evaluator), k = 2/(period+1).
 *   - SMA/Bollinger use a population standard deviation (divide by N, not N-1).
 *   - RSI/ATR use Wilder smoothing seeded by a simple average of the first `period` values.
 *   - Donchian/rollingHigh/rollingLow INCLUDE the current bar in the window.
 */

export interface Series {
  open: number[];
  high: number[];
  low: number[];
  close: number[];
  volume: number[];
}

const nan = (n: number): number[] => new Array(n).fill(NaN);

export function ema(prices: number[], period: number): number[] {
  const out = new Array(prices.length).fill(0);
  if (prices.length === 0) return out;
  const k = 2 / (period + 1);
  out[0] = prices[0];
  for (let i = 1; i < prices.length; i++) out[i] = prices[i] * k + out[i - 1] * (1 - k);
  return out;
}

// NaN-safe window mean: returns NaN until there are `period` consecutive finite values ending
// at i. A sliding-sum would be poisoned forever by the NaN warmup-prefix of a source indicator
// (e.g. Stochastic smoothing the raw-%K series), so each window is summed directly.
export function sma(prices: number[], period: number): number[] {
  const out = nan(prices.length);
  for (let i = period - 1; i < prices.length; i++) {
    let sum = 0;
    let ok = true;
    for (let j = i - period + 1; j <= i; j++) {
      if (!Number.isFinite(prices[j])) {
        ok = false;
        break;
      }
      sum += prices[j];
    }
    if (ok) out[i] = sum / period;
  }
  return out;
}

/** Population standard deviation over a trailing window of `period` values. */
function rollingStd(prices: number[], period: number, means: number[]): number[] {
  const out = nan(prices.length);
  for (let i = period - 1; i < prices.length; i++) {
    const mean = means[i];
    let acc = 0;
    for (let j = i - period + 1; j <= i; j++) acc += (prices[j] - mean) ** 2;
    out[i] = Math.sqrt(acc / period);
  }
  return out;
}

export function rsi(prices: number[], period: number): number[] {
  const out = nan(prices.length);
  if (prices.length <= period) return out;
  let avgGain = 0;
  let avgLoss = 0;
  // Seed with the simple average of the first `period` changes.
  for (let i = 1; i <= period; i++) {
    const change = prices[i] - prices[i - 1];
    if (change >= 0) avgGain += change;
    else avgLoss -= change;
  }
  avgGain /= period;
  avgLoss /= period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  // Wilder smoothing thereafter.
  for (let i = period + 1; i < prices.length; i++) {
    const change = prices[i] - prices[i - 1];
    const gain = change > 0 ? change : 0;
    const loss = change < 0 ? -change : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

export function atr(s: Series, period: number): number[] {
  const n = s.close.length;
  const out = nan(n);
  if (n <= period) return out;
  const tr = new Array(n).fill(0);
  tr[0] = s.high[0] - s.low[0];
  for (let i = 1; i < n; i++) {
    tr[i] = Math.max(
      s.high[i] - s.low[i],
      Math.abs(s.high[i] - s.close[i - 1]),
      Math.abs(s.low[i] - s.close[i - 1]),
    );
  }
  // Seed ATR at index `period` with the simple average of TR[1..period], then Wilder-smooth.
  let sum = 0;
  for (let i = 1; i <= period; i++) sum += tr[i];
  out[period] = sum / period;
  for (let i = period + 1; i < n; i++) out[i] = (out[i - 1] * (period - 1) + tr[i]) / period;
  return out;
}

export interface MacdResult {
  line: number[];
  signal: number[];
  hist: number[];
}
export function macd(prices: number[], fast: number, slow: number, signalPeriod: number): MacdResult {
  const emaFast = ema(prices, fast);
  const emaSlow = ema(prices, slow);
  const line = prices.map((_, i) => emaFast[i] - emaSlow[i]);
  const signal = ema(line, signalPeriod);
  const hist = line.map((v, i) => v - signal[i]);
  return { line, signal, hist };
}

export interface BandResult {
  upper: number[];
  mid: number[];
  lower: number[];
}
export function bollinger(prices: number[], period: number, mult: number): BandResult {
  const mid = sma(prices, period);
  const sd = rollingStd(prices, period, mid);
  const upper = mid.map((m, i) => m + mult * sd[i]);
  const lower = mid.map((m, i) => m - mult * sd[i]);
  return { upper, mid, lower };
}

export function rollingMax(values: number[], period: number): number[] {
  const out = nan(values.length);
  for (let i = period - 1; i < values.length; i++) {
    let m = -Infinity;
    for (let j = i - period + 1; j <= i; j++) if (values[j] > m) m = values[j];
    out[i] = m;
  }
  return out;
}

export function rollingMin(values: number[], period: number): number[] {
  const out = nan(values.length);
  for (let i = period - 1; i < values.length; i++) {
    let m = Infinity;
    for (let j = i - period + 1; j <= i; j++) if (values[j] < m) m = values[j];
    out[i] = m;
  }
  return out;
}

export interface DonchianResult {
  upper: number[];
  lower: number[];
}
export function donchian(s: Series, period: number): DonchianResult {
  return { upper: rollingMax(s.high, period), lower: rollingMin(s.low, period) };
}

export interface StochResult {
  k: number[];
  d: number[];
}
export function stochastic(s: Series, period: number, smoothK: number, smoothD: number): StochResult {
  const n = s.close.length;
  const hh = rollingMax(s.high, period);
  const ll = rollingMin(s.low, period);
  const rawK = nan(n);
  for (let i = period - 1; i < n; i++) {
    const range = hh[i] - ll[i];
    rawK[i] = range === 0 ? 0 : ((s.close[i] - ll[i]) / range) * 100;
  }
  const k = sma(rawK, smoothK);
  const d = sma(k, smoothD);
  return { k, d };
}
