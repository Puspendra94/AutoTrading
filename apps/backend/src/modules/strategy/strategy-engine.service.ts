import { Injectable, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { Strategy, StrategyStatus, ExecutionMode } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { Ticker, TickerStatus, OnboardingStage } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { LiveVsBacktestDivergence } from '../../entities/live-vs-backtest-divergence.entity';
import { LlmService } from '../llm/llm.service';
import { LlmPurpose } from '../../entities/llm-cost-log.entity';
import { StrategyEvaluatorService } from './strategy-evaluator.service';

@Injectable()
export class StrategyEngineService {
  constructor(
    @InjectRepository(Strategy)
    private readonly strategyRepo: Repository<Strategy>,
    @InjectRepository(BacktestResult)
    private readonly backtestRepo: Repository<BacktestResult>,
    @InjectRepository(StrategyEvaluationPolicy)
    private readonly policyRepo: Repository<StrategyEvaluationPolicy>,
    @InjectRepository(Ticker)
    private readonly tickerRepo: Repository<Ticker>,
    @InjectRepository(OhlcvData)
    private readonly ohlcvRepo: Repository<OhlcvData>,
    @InjectRepository(LiveVsBacktestDivergence)
    private readonly divergenceRepo: Repository<LiveVsBacktestDivergence>,
    private readonly evaluatorService: StrategyEvaluatorService,
    private readonly llmService: LlmService,
  ) {}

  async generateStrategyForTicker(tickerId: string) {
    const ticker = await this.tickerRepo.findOne({ where: { id: tickerId } });
    if (!ticker) throw new NotFoundException('Ticker not found');

    ticker.onboardingStage = OnboardingStage.GENERATING_STRATEGY;
    await this.tickerRepo.save(ticker);

    const candles = await this.ohlcvRepo.find({
      where: { tickerId },
      order: { timestamp: 'ASC' },
      take: 1000,
    });

    // 1. Calculate compact summary statistics for LLM context (Section 2.7)
    const latestPrice = candles.length > 0 ? Number(candles[candles.length - 1].close) : 0;
    const summaryPrompt = `Analyze ticker ${ticker.symbol} (Interval: ${ticker.interval}, Latest Price: ${latestPrice}). Propose optimal quantitative indicator parameters for an automated trend-following trading strategy. Respond in JSON.`;

    // 2. Request AI strategy proposal via LLMProvider
    const llmRes = await this.llmService.generateCompletion(summaryPrompt);
    let parsedParams: any;
    try {
      parsedParams = JSON.parse(llmRes.content);
    } catch {
      parsedParams = {
        indicatorConfig: { emaFastPeriod: 12, emaSlowPeriod: 26, stopLossPct: 1.5, takeProfitPct: 3.5 },
      };
    }

    // Determine strategy version
    const existingCount = await this.strategyRepo.count({ where: { tickerId } });
    const strategy = this.strategyRepo.create({
      tickerId,
      version: existingCount + 1,
      status: StrategyStatus.DRAFT,
      executionMode: ExecutionMode.MODE_A_RULES,
      parametersJson: parsedParams,
      generatedBy: llmRes.model,
    });
    await this.strategyRepo.save(strategy);

    // Log LLM cost
    await this.llmService.logCost(tickerId, strategy.id, LlmPurpose.STRATEGY_GENERATION, llmRes);

    // 3. Backtest & Strategy Evaluation Module Gate
    ticker.onboardingStage = OnboardingStage.BACKTESTING;
    await this.tickerRepo.save(ticker);

    let activePolicy = await this.policyRepo.findOne({ where: { isActive: true } });
    if (!activePolicy) {
      activePolicy = this.policyRepo.create({
        name: 'Default Policy',
        minSharpe: 1.0,
        maxDrawdownPct: 20.0,
        minProfitFactor: 1.3,
        minTradeCount: 100,
      });
    }

    const evalResult = this.evaluatorService.evaluateStrategy(candles, parsedParams, activePolicy);

    ticker.onboardingStage = OnboardingStage.EVALUATING;
    await this.tickerRepo.save(ticker);

    const backtest = this.backtestRepo.create({
      strategyId: strategy.id,
      sharpe: evalResult.sharpe,
      sortino: evalResult.sortino,
      calmar: evalResult.calmar,
      maxDrawdown: evalResult.maxDrawdown,
      drawdownDuration: evalResult.drawdownDuration,
      profitFactor: evalResult.profitFactor,
      tradeCount: evalResult.tradeCount,
      monteCarloSummaryJson: evalResult.monteCarloSummary,
      regimeBreakdownJson: evalResult.regimeBreakdown,
      passedEvaluationGate: evalResult.passedEvaluationGate,
    });
    await this.backtestRepo.save(backtest);

    // 4. Auto-promotion if passed evaluation gate (Section 2.5)
    if (evalResult.passedEvaluationGate) {
      // Retire previous active strategies for this ticker
      await this.strategyRepo.update(
        { tickerId, status: StrategyStatus.LIVE },
        { status: StrategyStatus.RETIRED },
      );

      strategy.status = StrategyStatus.LIVE;
      await this.strategyRepo.save(strategy);

      ticker.status = TickerStatus.ACTIVE;
      ticker.onboardingStage = OnboardingStage.READY;
      await this.tickerRepo.save(ticker);
    } else {
      strategy.status = StrategyStatus.EVALUATED;
      await this.strategyRepo.save(strategy);

      ticker.onboardingStage = OnboardingStage.FAILED;
      await this.tickerRepo.save(ticker);
    }

    return { strategy, backtest, evaluation: evalResult };
  }

  async evaluateRulesSignal(tickerId: string, currentCandle: OhlcvData): Promise<'BUY' | 'SELL' | 'HOLD'> {
    const liveStrategy = await this.strategyRepo.findOne({
      where: { tickerId, status: StrategyStatus.LIVE },
    });

    if (!liveStrategy) return 'HOLD';

    if (liveStrategy.executionMode === ExecutionMode.MODE_A_RULES) {
      // Fast Mode A rules engine signal check
      const params = liveStrategy.parametersJson;
      const emaFast = params.indicatorConfig?.emaFastPeriod || 12;
      const emaSlow = params.indicatorConfig?.emaSlowPeriod || 26;

      const candles = await this.ohlcvRepo.find({
        where: { tickerId },
        order: { timestamp: 'DESC' },
        take: emaSlow + 5,
      });

      if (candles.length < emaSlow) return 'HOLD';

      const closes = candles.map((c) => Number(c.close)).reverse();
      const last = closes[closes.length - 1];
      const prev = closes[closes.length - 2];

      if (last > prev * 1.002) return 'BUY';
      if (last < prev * 0.998) return 'SELL';
      return 'HOLD';
    }

    return 'HOLD';
  }

  async getActiveStrategyForTicker(tickerId: string) {
    const strategy = await this.strategyRepo.findOne({
      where: { tickerId, status: StrategyStatus.LIVE },
    });
    if (!strategy) return null;

    const backtest = await this.backtestRepo.findOne({ where: { strategyId: strategy.id } });
    return { strategy, backtest };
  }

  async getStrategiesByTicker(tickerId: string) {
    return this.strategyRepo.find({ where: { tickerId }, order: { version: 'DESC' } });
  }

  async getPolicies() {
    return this.policyRepo.find();
  }
}
