import { Controller, Get, Post, Param, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { StrategyEngineService } from './strategy-engine.service';

@Controller('strategies')
@UseGuards(AuthGuard('jwt'))
export class StrategyController {
  constructor(private readonly strategyEngineService: StrategyEngineService) {}

  @Post('ticker/:tickerId/generate')
  async generateStrategy(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.generateStrategyForTicker(tickerId);
  }

  @Get('ticker/:tickerId/active')
  async getActiveStrategy(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.getActiveStrategyForTicker(tickerId);
  }

  @Get('ticker/:tickerId/all')
  async getStrategiesByTicker(@Param('tickerId') tickerId: string) {
    return this.strategyEngineService.getStrategiesByTicker(tickerId);
  }

  @Get('policies')
  async getPolicies() {
    return this.strategyEngineService.getPolicies();
  }
}
