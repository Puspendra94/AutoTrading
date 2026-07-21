import { Injectable, Logger } from '@nestjs/common';
import { Cron, CronExpression } from '@nestjs/schedule';
import { StrategyEngineService } from '../modules/strategy/strategy-engine.service';

@Injectable()
export class StrategyReevaluationJob {
  private readonly logger = new Logger(StrategyReevaluationJob.name);

  constructor(private readonly strategyEngineService: StrategyEngineService) {}

  // Daily divergence check (spec 2.5/7.5/10) — flags + auto-regenerates any LIVE
  // strategy whose live performance has diverged significantly from its backtest.
  @Cron(CronExpression.EVERY_DAY_AT_1AM)
  async run() {
    try {
      const result = await this.strategyEngineService.runDivergenceCheckForAllLiveStrategies();
      this.logger.log(
        `Divergence check: ${result.checked} live strategies checked, ${result.flagged} flagged, ${result.regenerated} regenerated.`,
      );
    } catch (err) {
      this.logger.error(`Strategy re-evaluation job failed: ${err.message}`);
    }
  }
}
