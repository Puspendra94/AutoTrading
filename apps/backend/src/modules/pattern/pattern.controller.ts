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
}
