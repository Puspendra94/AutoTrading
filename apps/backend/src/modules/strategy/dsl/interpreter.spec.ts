import { simulateFromIR, Candle } from './interpreter';
import { legacyToIR, validateIR, countIRParameters, resolveIR, strategyTypeTag, StrategyIR } from './strategy-ir';
import { ema } from './indicators';
import { STRATEGY_GRAMMAR_PROMPT } from './grammar-prompt';

/**
 * Anchor test: the DSL interpreter, fed an auto-translated legacy EMA-crossover strategy, must
 * reproduce the pre-DSL engine's trades bar-for-bar. `legacyReferenceSimulate` below is a faithful
 * copy of strategy-evaluator.service.ts::simulateTrades (EMA-cross entry, SL/TP/cross-down exit,
 * 0.05% slippage each side, 0.15% round-trip fee) — if the interpreter drifts from it, this fails.
 */
function legacyReferenceSimulate(candles: Candle[], params: any) {
  const trades: number[] = [];
  let position: 'NONE' | 'LONG' = 'NONE';
  let entryPrice = 0;
  const cfg = params.indicatorConfig;
  const fast = cfg.emaFastPeriod;
  const slow = cfg.emaSlowPeriod;
  const sl = cfg.stopLossPct / 100;
  const tp = cfg.takeProfitPct / 100;
  const closes = candles.map((c) => Number(c.close));
  if (closes.length <= slow) return { trades };
  const fastEma = ema(closes, fast);
  const slowEma = ema(closes, slow);
  for (let i = slow; i < candles.length; i++) {
    const price = closes[i];
    if (position === 'NONE') {
      const bullish = fastEma[i] > slowEma[i] && fastEma[i - 1] <= slowEma[i - 1];
      if (bullish) {
        position = 'LONG';
        entryPrice = price * 1.0005;
      }
    } else {
      const ret = (price - entryPrice) / entryPrice;
      if (ret <= -sl || ret >= tp || fastEma[i] < slowEma[i]) {
        const exit = price * 0.9995;
        trades.push((exit - entryPrice) / entryPrice - 0.0015);
        position = 'NONE';
      }
    }
  }
  return { trades };
}

/** Deterministic OHLC random walk (seeded LCG) so the test is reproducible. */
function makeCandles(n: number, seed = 42): Candle[] {
  let state = seed;
  const rand = () => {
    state = (state * 1103515245 + 12345) & 0x7fffffff;
    return state / 0x7fffffff;
  };
  const candles: Candle[] = [];
  let price = 100;
  let t = 1_700_000_000_000;
  for (let i = 0; i < n; i++) {
    const drift = (rand() - 0.48) * 2; // slight upward bias so crossovers happen
    const close = Math.max(1, price + drift);
    const high = Math.max(price, close) + rand();
    const low = Math.min(price, close) - rand();
    candles.push({ open: price, high, low, close, volume: 100 + rand() * 10, timestamp: t });
    price = close;
    t += 3_600_000;
  }
  return candles;
}

describe('DSL interpreter parity with legacy EMA crossover', () => {
  it('reproduces the pre-DSL trades exactly for an auto-translated legacy strategy', () => {
    const candles = makeCandles(1500);
    const legacyParams = { indicatorConfig: { emaFastPeriod: 12, emaSlowPeriod: 26, stopLossPct: 1.5, takeProfitPct: 3.5 } };

    const reference = legacyReferenceSimulate(candles, legacyParams);
    const viaIR = simulateFromIR(candles, legacyToIR(legacyParams));

    expect(viaIR.trades.length).toBe(reference.trades.length);
    expect(viaIR.trades.length).toBeGreaterThan(3); // sanity: the fixture actually trades
    for (let i = 0; i < reference.trades.length; i++) {
      expect(viaIR.trades[i]).toBeCloseTo(reference.trades[i], 12);
    }
  });
});

describe('DSL validation + arbitrary rule trees', () => {
  it('accepts a valid RSI mean-reversion tree and runs it', () => {
    const ir: StrategyIR = {
      strategyName: 'RSI dip in uptrend',
      reasoning: 'buy oversold while above the 200 EMA',
      entry: {
        op: 'and',
        conditions: [
          { op: 'lt', left: { op: 'indicator', kind: 'rsi', period: 14 }, right: { op: 'const', value: 30 } },
          { op: 'gt', left: { op: 'price', field: 'close' }, right: { op: 'indicator', kind: 'ema', period: 200 } },
        ],
      },
      exit: { op: 'gt', left: { op: 'indicator', kind: 'rsi', period: 14 }, right: { op: 'const', value: 55 } },
      risk: { stopLossPct: 2, takeProfitPct: 4 },
    };
    expect(validateIR(ir)).toEqual([]);
    // Distinct knobs (rsi14 is reused in entry+exit -> counted once): rsi14 + const30 + ema200
    // + const55 + stopLoss + takeProfit = 6.
    expect(countIRParameters(ir)).toBe(6);
    const res = simulateFromIR(makeCandles(2000), ir);
    expect(Array.isArray(res.trades)).toBe(true);
  });

  it('rejects an invalid tree (unknown indicator / bad period)', () => {
    const bad: any = {
      strategyName: 'x',
      reasoning: '',
      entry: { op: 'gt', left: { op: 'indicator', kind: 'wobble', period: 5 }, right: { op: 'const', value: 1 } },
      exit: { op: 'lt', left: { op: 'indicator', kind: 'ema', period: 1 }, right: { op: 'const', value: 1 } },
      risk: { stopLossPct: 2, takeProfitPct: 4 },
    };
    expect(validateIR(bad).length).toBeGreaterThan(0);
  });

  it('tags strategies by their distinct indicator kinds', () => {
    expect(strategyTypeTag(legacyToIR({ indicatorConfig: { emaFastPeriod: 12, emaSlowPeriod: 26 } }))).toBe('ema');
    const rsiEma: StrategyIR = {
      strategyName: 't', reasoning: '',
      entry: { op: 'lt', left: { op: 'indicator', kind: 'rsi', period: 14 }, right: { op: 'const', value: 30 } },
      exit: { op: 'crossBelow', left: { op: 'indicator', kind: 'ema', period: 10 }, right: { op: 'indicator', kind: 'ema', period: 30 } },
      risk: { stopLossPct: 2, takeProfitPct: 4 },
    };
    expect(strategyTypeTag(rsiEma)).toBe('ema_rsi');
  });
});

describe('generation round-trip (LLM output string -> IR)', () => {
  // Mimics parseGeneratedStrategy: the LLM returns the tree as a JSON string; we parse + validate.
  it('parses a rule-tree JSON string, validates, and backtests', () => {
    const llmOutput = {
      strategyName: 'MACD momentum',
      reasoning: 'ride momentum',
      strategy: JSON.stringify({
        entry: { op: 'crossAbove', left: { op: 'indicator', kind: 'macd', fast: 12, slow: 26, signal: 9, field: 'line' }, right: { op: 'indicator', kind: 'macd', fast: 12, slow: 26, signal: 9, field: 'signal' } },
        exit: { op: 'lt', left: { op: 'indicator', kind: 'macd', fast: 12, slow: 26, signal: 9, field: 'hist' }, right: { op: 'const', value: 0 } },
        risk: { stopLossPct: 3, takeProfitPct: 6 },
      }),
    };
    const body = JSON.parse(llmOutput.strategy);
    const ir = { strategyName: llmOutput.strategyName, reasoning: llmOutput.reasoning, ...body } as StrategyIR;
    expect(validateIR(ir)).toEqual([]);
    expect(resolveIR(ir)).not.toBeNull();
    expect(Array.isArray(simulateFromIR(makeCandles(2000), ir).trades)).toBe(true);
  });

  it('the example rule trees shown to the LLM in the grammar prompt are all valid', () => {
    // Guards the few-shot examples: extract each JSON object beginning with {"entry" and validate.
    const examples = STRATEGY_GRAMMAR_PROMPT.match(/\{"entry".*$/gm) || [];
    expect(examples.length).toBeGreaterThanOrEqual(2);
    for (const ex of examples) {
      const body = JSON.parse(ex);
      const ir = { strategyName: 'x', reasoning: '', ...body } as StrategyIR;
      expect(validateIR(ir)).toEqual([]);
    }
  });
});
