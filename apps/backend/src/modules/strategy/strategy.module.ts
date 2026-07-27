import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Strategy } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { Ticker } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { LiveVsBacktestDivergence } from '../../entities/live-vs-backtest-divergence.entity';
import { Position } from '../../entities/position.entity';
import { AiLessonLearned } from '../../entities/ai-lesson-learned.entity';
import { StrategyPerformance } from '../../entities/strategy-performance.entity';
import { LlmModule } from '../llm/llm.module';
import { MarketDataModule } from '../market-data/market-data.module';
import { RiskExecutionModule } from '../risk-execution/risk-execution.module';
import { StrategyEvaluatorService } from './strategy-evaluator.service';
import { StrategyEngineService } from './strategy-engine.service';
import { StrategyPerformanceService } from './strategy-performance.service';
import { StrategyController } from './strategy.controller';
import { AiLessonsService } from './ai-lessons.service';
import { StrategyRegenerateConsumer } from './strategy-regenerate.consumer';
import { SignalsBridgeService } from './signals-bridge.service';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      Strategy,
      BacktestResult,
      StrategyEvaluationPolicy,
      Ticker,
      OhlcvData,
      LiveVsBacktestDivergence,
      Position,
      AiLessonLearned,
      StrategyPerformance,
    ]),
    LlmModule,
    MarketDataModule,
    RiskExecutionModule,
  ],
  providers: [StrategyEvaluatorService, StrategyEngineService, StrategyPerformanceService, AiLessonsService, StrategyRegenerateConsumer, SignalsBridgeService],
  controllers: [StrategyController],
  exports: [StrategyEvaluatorService, StrategyEngineService, StrategyPerformanceService, AiLessonsService],
})
export class StrategyModule {}
