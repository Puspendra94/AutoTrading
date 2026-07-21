import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { Strategy } from '../../entities/strategy.entity';
import { BacktestResult } from '../../entities/backtest-result.entity';
import { StrategyEvaluationPolicy } from '../../entities/strategy-evaluation-policy.entity';
import { Ticker } from '../../entities/ticker.entity';
import { OhlcvData } from '../../entities/ohlcv-data.entity';
import { LiveVsBacktestDivergence } from '../../entities/live-vs-backtest-divergence.entity';
import { LlmModule } from '../llm/llm.module';
import { StrategyEvaluatorService } from './strategy-evaluator.service';
import { StrategyEngineService } from './strategy-engine.service';
import { StrategyController } from './strategy.controller';

@Module({
  imports: [
    TypeOrmModule.forFeature([
      Strategy,
      BacktestResult,
      StrategyEvaluationPolicy,
      Ticker,
      OhlcvData,
      LiveVsBacktestDivergence,
    ]),
    LlmModule,
  ],
  providers: [StrategyEvaluatorService, StrategyEngineService],
  controllers: [StrategyController],
  exports: [StrategyEvaluatorService, StrategyEngineService],
})
export class StrategyModule {}
