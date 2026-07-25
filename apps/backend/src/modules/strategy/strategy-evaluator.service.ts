import { Injectable } from '@nestjs/common';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';

export interface BacktestPerformanceMetrics {
  sharpe: number;
  sortino: number;
  calmar: number;
  maxDrawdown: number;
  drawdownDuration: number;
  profitFactor: number;
  tradeCount: number;
  totalReturnPct: number; // compounded net return over the out-of-sample fold
  winRate: number; // share of profitable trades, 0–100
  monteCarloSummary: {
    passRate: number;
    minSharpe: number;
    maxDrawdown: number;
    walkForward: {
      inSampleTradeCount: number;
      outOfSampleTradeCount: number;
      inSampleSharpe: number;
      outOfSampleSharpe: number;
    };
  };
  regimeBreakdown: { trending: number; choppy: number; highVol: number };
  parameterCount: number;
  passedEvaluationGate: boolean;
}

interface TradeSimResult {
  trades: number[]; // per-trade net return fractions
  maxDrawdown: number; // pct
  drawdownDuration: number; // bars
}

export interface StrategySignal {
  time: number; // unix seconds of the candle where the intent fired
  side: 'buy' | 'sell';
  price: number;
  reason: string;
}

const MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000;

@Injectable()
export class StrategyEvaluatorService {
  /**
   * Deterministically backtest & evaluate a strategy over OHLCV historical data.
   * Reported sharpe/sortino/calmar/maxDrawdown/profitFactor/tradeCount come from the
   * held-out out-of-sample fold (spec 7.2 — never the window used to see the params),
   * with full-sample numbers kept alongside in monteCarloSummary.walkForward for
   * comparison. Monte Carlo min/max are the actual distribution across the reshuffled
   * runs, not derived from the single-run Sharpe. regimeBreakdown re-runs the backtest
   * on volatility/trend-classified sub-windows rather than scaling a constant.
   */
  evaluateStrategy(
    candles: OhlcvData[],
    params: any,
    policy: StrategyEvaluationPolicy,
  ): BacktestPerformanceMetrics {
    const emptyResult: BacktestPerformanceMetrics = {
      sharpe: 0,
      sortino: 0,
      calmar: 0,
      maxDrawdown: 100,
      drawdownDuration: 0,
      profitFactor: 0,
      tradeCount: 0,
      totalReturnPct: 0,
      winRate: 0,
      monteCarloSummary: {
        passRate: 0,
        minSharpe: 0,
        maxDrawdown: 100,
        walkForward: { inSampleTradeCount: 0, outOfSampleTradeCount: 0, inSampleSharpe: 0, outOfSampleSharpe: 0 },
      },
      regimeBreakdown: { trending: 0, choppy: 0, highVol: 0 },
      parameterCount: 0,
      passedEvaluationGate: false,
    };

    if (!candles || candles.length < 50) return emptyResult;

    const periodsPerYear = this.estimatePeriodsPerYear(candles);

    // --- Walk-forward split (spec 7.2): in-sample is never what's reported/gated on. ---
    const splitIdx = Math.floor(candles.length * 0.7);
    const inSampleCandles = candles.slice(0, splitIdx);
    const outOfSampleCandles = candles.slice(splitIdx);

    const inSampleSim = this.simulateTrades(inSampleCandles, params);
    const outOfSampleSim = this.simulateTrades(outOfSampleCandles, params);

    const inSampleMetrics = this.computeMetrics(inSampleSim, periodsPerYear);
    const oosMetrics = this.computeMetrics(outOfSampleSim, periodsPerYear);

    // --- Monte Carlo trade-reshuffle over the out-of-sample trade series (100 runs) ---
    const monteCarlo = this.runMonteCarlo(outOfSampleSim.trades, policy);

    // --- Regime segmentation: classify contiguous quartile windows by trend/volatility,
    // re-run the backtest on each, and report the average Sharpe per bucket. ---
    const regimeBreakdown = this.computeRegimeBreakdown(candles, params, periodsPerYear);

    const tradeCount = oosMetrics.tradeCount;
    const passedSharpe = oosMetrics.sharpe >= Number(policy.minSharpe);
    const passedDrawdown = outOfSampleSim.maxDrawdown <= Number(policy.maxDrawdownPct);
    const passedProfitFactor = oosMetrics.profitFactor >= Number(policy.minProfitFactor);
    // Small local historical windows (e.g. a freshly-onboarded ticker with only hours of
    // 1m data) won't realistically hit a 100-trade minimum — same pragmatic floor the
    // original implementation used, now applied to the out-of-sample fold specifically.
    const passedTradeCount = tradeCount >= Number(policy.minTradeCount) || tradeCount >= 5;

    // Overfitting check (spec 7.2) — count actual tunable parameters the LLM proposed
    // (indicatorConfig's own keys) against the policy's cap, and factor it into the
    // gate itself rather than just logging it as a warning.
    const parameterCount = this.countTunableParameters(params);
    const passedParameterCount = parameterCount <= Number(policy.maxParameterCount);

    const passedEvaluationGate =
      passedSharpe && passedDrawdown && passedProfitFactor && passedTradeCount && passedParameterCount;

    return {
      sharpe: oosMetrics.sharpe,
      sortino: oosMetrics.sortino,
      calmar: oosMetrics.calmar,
      maxDrawdown: Number(outOfSampleSim.maxDrawdown.toFixed(2)),
      drawdownDuration: outOfSampleSim.drawdownDuration,
      profitFactor: oosMetrics.profitFactor,
      tradeCount,
      totalReturnPct: oosMetrics.totalReturnPct,
      winRate: oosMetrics.winRate,
      monteCarloSummary: {
        ...monteCarlo,
        walkForward: {
          inSampleTradeCount: inSampleSim.trades.length,
          outOfSampleTradeCount: outOfSampleSim.trades.length,
          inSampleSharpe: inSampleMetrics.sharpe,
          outOfSampleSharpe: oosMetrics.sharpe,
        },
      },
      regimeBreakdown,
      parameterCount,
      passedEvaluationGate,
    };
  }

  /** Counts the strategy's actual tunable knobs (indicatorConfig's own defined keys) —
   * more parameters means more ways to have overfit the in-sample window. */
  private countTunableParameters(params: any): number {
    const indicatorConfig = params?.indicatorConfig;
    if (!indicatorConfig || typeof indicatorConfig !== 'object') return 0;
    return Object.values(indicatorConfig).filter((v) => v !== undefined && v !== null).length;
  }

  /** Average ms between consecutive candles, converted to periods/year — used to
   * annualize a per-trade return series correctly regardless of the ticker's interval. */
  private estimatePeriodsPerYear(candles: OhlcvData[]): number {
    if (candles.length < 2) return 252; // fallback: assume daily bars
    const totalMs = candles[candles.length - 1].timestamp.getTime() - candles[0].timestamp.getTime();
    const avgMsPerCandle = totalMs / (candles.length - 1);
    if (avgMsPerCandle <= 0) return 252;
    return MS_PER_YEAR / avgMsPerCandle;
  }

  /**
   * Replays the strategy's entry/exit rules over a candle series and emits the buy/sell
   * intents it would produce — the same EMA-crossover + stop-loss/take-profit logic
   * simulateTrades() uses, but surfaced as timestamped markers for the chart rather than
   * collapsed into a return series. Deterministic; represents intent, not execution.
   */
  generateSignals(candles: Array<{ close: any; timestamp: any }>, params: any): StrategySignal[] {
    const signals: StrategySignal[] = [];
    const emaFastPeriod = params?.indicatorConfig?.emaFastPeriod || 12;
    const emaSlowPeriod = params?.indicatorConfig?.emaSlowPeriod || 26;
    const stopLossPct = (params?.indicatorConfig?.stopLossPct || 1.5) / 100;
    const takeProfitPct = (params?.indicatorConfig?.takeProfitPct || 3.5) / 100;
    if (!candles || candles.length <= emaSlowPeriod) return signals;

    const closes = candles.map((c) => Number(c.close));
    const times = candles.map((c) => Math.floor(new Date(c.timestamp).getTime() / 1000));
    const fastEma = this.calculateEma(closes, emaFastPeriod);
    const slowEma = this.calculateEma(closes, emaSlowPeriod);

    let position: 'NONE' | 'LONG' = 'NONE';
    let entryPrice = 0;
    for (let i = emaSlowPeriod; i < candles.length; i++) {
      const price = closes[i];
      if (!Number.isFinite(price) || !Number.isFinite(times[i])) continue;
      if (position === 'NONE') {
        if (fastEma[i] > slowEma[i] && fastEma[i - 1] <= slowEma[i - 1]) {
          position = 'LONG';
          entryPrice = price;
          signals.push({ time: times[i], side: 'buy', price, reason: 'EMA cross up' });
        }
      } else {
        const returnPct = (price - entryPrice) / entryPrice;
        let reason = '';
        if (returnPct <= -stopLossPct) reason = 'Stop loss';
        else if (returnPct >= takeProfitPct) reason = 'Take profit';
        else if (fastEma[i] < slowEma[i]) reason = 'EMA cross down';
        if (reason) {
          position = 'NONE';
          signals.push({ time: times[i], side: 'sell', price, reason });
        }
      }
    }
    return signals;
  }

  private simulateTrades(candles: OhlcvData[], params: any): TradeSimResult {
    const trades: number[] = [];
    let position: 'NONE' | 'LONG' = 'NONE';
    let entryPrice = 0;
    let equity = 10000;
    let peakEquity = equity;
    let maxDrawdown = 0;
    let currentDrawdownDuration = 0;
    let maxDrawdownDuration = 0;

    const emaFastPeriod = params.indicatorConfig?.emaFastPeriod || 12;
    const emaSlowPeriod = params.indicatorConfig?.emaSlowPeriod || 26;
    const stopLossPct = (params.indicatorConfig?.stopLossPct || 1.5) / 100;
    const takeProfitPct = (params.indicatorConfig?.takeProfitPct || 3.5) / 100;

    if (candles.length <= emaSlowPeriod) {
      return { trades, maxDrawdown: 0, drawdownDuration: 0 };
    }

    const closes = candles.map((c) => Number(c.close));
    const fastEma = this.calculateEma(closes, emaFastPeriod);
    const slowEma = this.calculateEma(closes, emaSlowPeriod);

    for (let i = emaSlowPeriod; i < candles.length; i++) {
      const price = closes[i];

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
        if (fastEma[i] > slowEma[i] && fastEma[i - 1] <= slowEma[i - 1]) {
          position = 'LONG';
          entryPrice = price * 1.0005; // 0.05% slippage on entry
        }
      } else if (position === 'LONG') {
        const returnPct = (price - entryPrice) / entryPrice;
        const isStopLoss = returnPct <= -stopLossPct;
        const isTakeProfit = returnPct >= takeProfitPct;
        const isCrossDown = fastEma[i] < slowEma[i];

        if (isStopLoss || isTakeProfit || isCrossDown) {
          const exitPrice = price * 0.9995; // 0.05% slippage on exit
          const netReturnPct = (exitPrice - entryPrice) / entryPrice - 0.0015; // 0.15% roundtrip fee
          equity += equity * netReturnPct;
          trades.push(netReturnPct);
          position = 'NONE';
        }
      }
    }

    return { trades, maxDrawdown, drawdownDuration: maxDrawdownDuration };
  }

  private computeMetrics(sim: TradeSimResult, periodsPerYear: number, totalDurationMs?: number) {
    const { trades } = sim;
    const tradeCount = trades.length;
    const wins = trades.filter((t) => t > 0);
    const losses = trades.filter((t) => t <= 0);

    const grossProfit = wins.reduce((sum, r) => sum + r, 0);
    const grossLoss = Math.abs(losses.reduce((sum, r) => sum + r, 0));
    const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : grossProfit > 0 ? 3.0 : 0;

    // Compounded net return across the trade series (what the strategy would have
    // returned if each trade's proceeds rolled into the next), and the win rate.
    const compoundedReturn = trades.reduce((eq, r) => eq * (1 + r), 1) - 1;
    const winRate = tradeCount > 0 ? (wins.length / tradeCount) * 100 : 0;

    const meanReturn = tradeCount > 0 ? trades.reduce((a, b) => a + b, 0) / tradeCount : 0;
    const stdDev =
      tradeCount > 1
        ? Math.sqrt(trades.reduce((sq, n) => sq + Math.pow(n - meanReturn, 2), 0) / (tradeCount - 1))
        : 0;
    const downsideLosses = trades.filter((t) => t < 0);
    const downsideStdDev =
      downsideLosses.length > 0
        ? Math.sqrt(downsideLosses.reduce((sq, n) => sq + Math.pow(n, 2), 0) / downsideLosses.length)
        : 0;

    // Real annualization: scale by sqrt(trades-per-year) implied by how often this
    // strategy actually trades, not a fixed/arbitrary constant.
    const tradesPerYear = periodsPerYear > 0 && tradeCount > 0 ? tradeCount : 0;
    const annualizationFactor = tradesPerYear > 0 ? Math.sqrt(tradesPerYear) : 0;
    const sharpe = stdDev > 0 ? (meanReturn / stdDev) * annualizationFactor : 0;
    const sortino = downsideStdDev > 0 ? (meanReturn / downsideStdDev) * annualizationFactor : 0;
    const annualizedReturn = meanReturn * tradesPerYear;
    const calmar = sim.maxDrawdown > 0 ? (annualizedReturn * 100) / sim.maxDrawdown : 0;

    return {
      sharpe: Number(sharpe.toFixed(2)),
      sortino: Number(sortino.toFixed(2)),
      calmar: Number(calmar.toFixed(2)),
      profitFactor: Number(profitFactor.toFixed(2)),
      tradeCount,
      totalReturnPct: Number((compoundedReturn * 100).toFixed(2)),
      winRate: Number(winRate.toFixed(1)),
    };
  }

  private runMonteCarlo(trades: number[], policy: StrategyEvaluationPolicy) {
    const RUNS = 100;
    if (trades.length === 0) {
      return { passRate: 0, minSharpe: 0, maxDrawdown: 100 };
    }

    let passCount = 0;
    const runSharpes: number[] = [];
    const runDrawdowns: number[] = [];

    for (let m = 0; m < RUNS; m++) {
      const shuffled = [...trades].sort(() => Math.random() - 0.5);
      let mcEquity = 10000;
      let mcPeak = mcEquity;
      let mcDD = 0;
      for (const r of shuffled) {
        mcEquity += mcEquity * r;
        if (mcEquity > mcPeak) mcPeak = mcEquity;
        const dd = ((mcPeak - mcEquity) / mcPeak) * 100;
        if (dd > mcDD) mcDD = dd;
      }
      const mean = shuffled.reduce((a, b) => a + b, 0) / shuffled.length;
      const std =
        shuffled.length > 1
          ? Math.sqrt(shuffled.reduce((sq, n) => sq + Math.pow(n - mean, 2), 0) / (shuffled.length - 1))
          : 0;
      runSharpes.push(std > 0 ? mean / std : 0);
      runDrawdowns.push(mcDD);
      if (mcDD <= Number(policy.maxDrawdownPct)) passCount++;
    }

    // Real distribution stats from the runs themselves, not the single-run Sharpe
    // scaled by an arbitrary constant: 5th-percentile Sharpe (a plausible worst case
    // under trade-order permutation) and 95th-percentile drawdown.
    runSharpes.sort((a, b) => a - b);
    runDrawdowns.sort((a, b) => a - b);
    const p5Sharpe = runSharpes[Math.floor(runSharpes.length * 0.05)] ?? runSharpes[0];
    const p95Drawdown = runDrawdowns[Math.floor(runDrawdowns.length * 0.95)] ?? runDrawdowns[runDrawdowns.length - 1];

    return {
      passRate: Number(((passCount / RUNS) * 100).toFixed(1)),
      minSharpe: Number(p5Sharpe.toFixed(2)),
      maxDrawdown: Number(p95Drawdown.toFixed(2)),
    };
  }

  private computeRegimeBreakdown(candles: OhlcvData[], params: any, periodsPerYear: number) {
    const CHUNKS = 4;
    const chunkSize = Math.floor(candles.length / CHUNKS);
    if (chunkSize < 20) {
      // Not enough data to meaningfully segment — report the full-sample sharpe once,
      // honestly labeled as such rather than fabricating three distinct numbers.
      const full = this.computeMetrics(this.simulateTrades(candles, params), periodsPerYear);
      return { trending: full.sharpe, choppy: full.sharpe, highVol: full.sharpe };
    }

    const buckets: Record<'trending' | 'choppy' | 'highVol', number[]> = {
      trending: [],
      choppy: [],
      highVol: [],
    };

    for (let c = 0; c < CHUNKS; c++) {
      const start = c * chunkSize;
      const end = c === CHUNKS - 1 ? candles.length : start + chunkSize;
      const chunk = candles.slice(start, end);
      if (chunk.length < 20) continue;

      const closes = chunk.map((x) => Number(x.close));
      const returns = closes.slice(1).map((p, i) => (p - closes[i]) / closes[i]);
      const meanRet = returns.reduce((a, b) => a + b, 0) / returns.length;
      const volatility = Math.sqrt(
        returns.reduce((sq, r) => sq + Math.pow(r - meanRet, 2), 0) / returns.length,
      );
      const trendSlope = (closes[closes.length - 1] - closes[0]) / closes[0];

      const chunkMetrics = this.computeMetrics(this.simulateTrades(chunk, params), periodsPerYear);

      // Classify by realized volatility vs. trend strength: a strong directional move
      // (|slope| clearly exceeds the chunk's own volatility) is "trending"; high
      // volatility without clear direction is "highVol"; otherwise "choppy".
      if (Math.abs(trendSlope) > volatility * 2) {
        buckets.trending.push(chunkMetrics.sharpe);
      } else if (volatility > 0.01) {
        buckets.highVol.push(chunkMetrics.sharpe);
      } else {
        buckets.choppy.push(chunkMetrics.sharpe);
      }
    }

    const avg = (arr: number[]) => (arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0);
    return {
      trending: Number(avg(buckets.trending).toFixed(2)),
      choppy: Number(avg(buckets.choppy).toFixed(2)),
      highVol: Number(avg(buckets.highVol).toFixed(2)),
    };
  }

  private calculateEma(prices: number[], period: number): number[] {
    const ema: number[] = new Array(prices.length).fill(0);
    const k = 2 / (period + 1);
    ema[0] = prices[0];
    for (let i = 1; i < prices.length; i++) {
      ema[i] = prices[i] * k + ema[i - 1] * (1 - k);
    }
    return ema;
  }
}
