import { Controller, Get, Param, Query, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { PatternService } from './pattern.service';

@Controller('pattern')
@UseGuards(AuthGuard('jwt'))
export class PatternController {
  constructor(private readonly patternService: PatternService) {}

  /** Chart markers for the pattern brain's triggers, bucketed onto the display interval. */
  @Get('ticker/:tickerId/markers')
  async getMarkers(
    @Param('tickerId') tickerId: string,
    @Query('interval') interval?: string,
    @Query('limit') limit?: string,
  ) {
    const parsed = limit ? parseInt(limit, 10) : NaN;
    const lim = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), 2000) : 500;
    return { markers: await this.patternService.getMarkers(tickerId, interval || '15m', lim) };
  }

  /** Latest feature state — regime, S/R levels, indicators — for the dashboard strip. */
  @Get('ticker/:tickerId/state')
  async getState(@Param('tickerId') tickerId: string) {
    return { state: await this.patternService.getLatestState(tickerId) };
  }

  /**
   * The decision feed: what the brain did on each evaluated bar and why.
   * `includeSkips=true` also returns the free "no trigger" bars.
   */
  @Get('ticker/:tickerId/decisions')
  async getDecisions(
    @Param('tickerId') tickerId: string,
    @Query('limit') limit?: string,
    @Query('includeSkips') includeSkips?: string,
  ) {
    const parsed = limit ? parseInt(limit, 10) : NaN;
    const lim = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), 500) : 50;
    return { decisions: await this.patternService.getDecisions(tickerId, lim, includeSkips === 'true') };
  }

  /** Outcome counts + LLM spend over a window, for the header strip. */
  @Get('ticker/:tickerId/decisions/summary')
  async getDecisionSummary(@Param('tickerId') tickerId: string, @Query('hours') hours?: string) {
    const parsed = hours ? parseInt(hours, 10) : NaN;
    const window = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 1), 720) : 24;
    return this.patternService.getDecisionSummary(tickerId, window);
  }
}
