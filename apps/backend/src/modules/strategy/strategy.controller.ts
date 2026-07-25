import { Controller, Get, Post, Param, Query, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { StrategyEngineService } from './strategy-engine.service';
import { StrategyPerformanceService } from './strategy-performance.service';
import { AiLessonsService } from './ai-lessons.service';

@Controller('strategies')
@UseGuards(AuthGuard('jwt'))
export class StrategyController {
  constructor(
    private readonly strategyEngineService: StrategyEngineService,
    private readonly performanceService: StrategyPerformanceService,
    private readonly aiLessonsService: AiLessonsService,
  ) {}

  @Get('ticker/:tickerId/lessons')
  async getLessons(@Param('tickerId') tickerId: string) {
    return this.aiLessonsService.retrieveRelevantLessons(tickerId, undefined, 20);
  }

  @Post('ticker/:tickerId/generate')
  async generateStrategy(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.generateStrategyForTicker(tickerId);
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
    return this.strategyEngineService.getSignalsForTicker(tickerId, interval || '15m', limit ? parseInt(limit, 10) : 500);
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
