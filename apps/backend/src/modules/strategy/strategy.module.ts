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
import { LlmModule } from '../llm/llm.module';
import { StrategyEvaluatorService } from './strategy-evaluator.service';
import { StrategyEngineService } from './strategy-engine.service';
import { StrategyController } from './strategy.controller';
import { AiLessonsService } from './ai-lessons.service';

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
    ]),
    LlmModule,
  ],
  providers: [StrategyEvaluatorService, StrategyEngineService, AiLessonsService],
  controllers: [StrategyController],
  exports: [StrategyEvaluatorService, StrategyEngineService, AiLessonsService],
})
export class StrategyModule {}
