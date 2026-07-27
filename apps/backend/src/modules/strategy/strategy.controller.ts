import { Controller, Get, Post, Param, Query, UseGuards, Inject } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { randomUUID } from 'crypto';
import Redis from 'ioredis';
import { StrategyEngineService } from './strategy-engine.service';
import { StrategyPerformanceService } from './strategy-performance.service';
import { AiLessonsService } from './ai-lessons.service';
import { SignalsBridgeService } from './signals-bridge.service';
import { REDIS_PUBLISHER } from '../../common/redis/redis.module';
import { publishJson } from '../../common/redis/redis-publish.util';
import { config } from '../../config/configuration';

const STRATEGY_GENERATE_CHANNEL = 'strategy:generate';

@Controller('strategies')
@UseGuards(AuthGuard('jwt'))
export class StrategyController {
  constructor(
    private readonly strategyEngineService: StrategyEngineService,
    private readonly performanceService: StrategyPerformanceService,
    private readonly aiLessonsService: AiLessonsService,
    private readonly signalsBridge: SignalsBridgeService,
    @Inject(REDIS_PUBLISHER) private readonly redis: Redis,
  ) {}

  @Get('ticker/:tickerId/lessons')
  async getLessons(@Param('tickerId') tickerId: string) {
    return this.aiLessonsService.retrieveRelevantLessons(tickerId, undefined, 20);
  }

  // Optional `interval` (e.g. 5m, 15m, 1h, 4h, 1d) chooses the generation/backtest/live timeframe
  // for this run — handy for fast iteration. Omitted -> the adaptive planner picks it.
  // `skipGate=true` promotes the first valid strategy even if it FAILS the evaluation gate —
  // testing only, so the execution loop can be exercised on timeframes where nothing clears the bar.
  @Post('ticker/:tickerId/generate')
  async generateStrategy(
    @Param('tickerId') tickerId: string,
    @Query('interval') interval?: string,
    @Query('skipGate') skipGate?: string,
  ) {
    // The worker owns the strategy engine: hand the job to it over Redis and return immediately;
    // the result arrives on the websocket ('strategy_generation_complete').
    const requestId = randomUUID();
    publishJson(this.redis, STRATEGY_GENERATE_CHANNEL, {
      requestId,
      tickerId,
      interval,
      skipGate: skipGate === 'true',
      reason: 'manual generation',
    });
    return { status: 'generating', requestId };
  }

  @Get('ticker/:tickerId/active')
  async getActiveStrategy(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.getActiveStrategyForTicker(tickerId);
  }

  @Get('ticker/:tickerId/signals')
  async getSignals(
    @Param('tickerId') tickerId: string,
    @Query('interval') interval?: string,
    @Query('limit') limit?: string,
  ) {
    const iv = interval || '15m';
    const lim = limit ? parseInt(limit, 10) : 500;
    // The worker owns the DSL engine — request the chart markers from it over Redis.
    return this.signalsBridge.requestSignals(tickerId, iv, lim);
  }

  @Get('ticker/:tickerId/all')
  async getStrategiesByTicker(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.getStrategiesByTicker(tickerId);
  }

  @Post(':strategyId/activate')
  async activateStrategy(@Param('strategyId') strategyId: string) {
    return this.strategyEngineService.activateStrategy(strategyId);
  }

  // Full-history "real data" performance replay. If it's never been computed, kick it off
  // in the background and report it as pending so the client can poll.
  @Get(':strategyId/performance')
  async getPerformance(@Param('strategyId') strategyId: string) {
    const perf = await this.performanceService.getPerformance(strategyId);
    if (!perf) {
      this.performanceService.computeForStrategyInBackground(strategyId);
      return { strategyId, status: 'pending' };
    }
    return perf;
  }

  @Post(':strategyId/performance/recompute')
  async recomputePerformance(@Param('strategyId') strategyId: string) {
    this.performanceService.computeForStrategyInBackground(strategyId);
    return { strategyId, status: 'pending' };
  }

  @Get('policies')
  async getPolicies() {
    return this.strategyEngineService.getPolicies();
  }
}
