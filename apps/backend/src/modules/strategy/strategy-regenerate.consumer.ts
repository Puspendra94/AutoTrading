import { Inject, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import Redis from 'ioredis';
import { REDIS_SUBSCRIBER } from '../../common/redis/redis.module';
import { StrategyEngineService } from './strategy-engine.service';

// Must match worker-py's redis_bus.STRATEGY_REGENERATE_CHANNEL.
export const STRATEGY_REGENERATE_CHANNEL = 'strategy:regenerate';

/**
 * Phase 3a (see MIGRATION.md): the worker's Strategy Supervisor decides — cheaply and
 * without any AI — *when* a live strategy has drifted enough to be replaced, and publishes
 * here. This consumer runs the one proven, audited generation path (the same
 * generateStrategyForTicker the daily divergence job uses), so there is still a single
 * implementation of strategy generation. It moves into the worker in Phase 3b.
 */
@Injectable()
export class StrategyRegenerateConsumer implements OnModuleInit {
  private readonly logger = new Logger(StrategyRegenerateConsumer.name);
  // Tickers with a regeneration in flight — generation is slow (backtest + LLM), so we
  // never run two at once for the same ticker if triggers arrive close together.
  private readonly inFlight = new Set<string>();

  constructor(
    @Inject(REDIS_SUBSCRIBER) private readonly subscriber: Redis,
    private readonly strategyEngine: StrategyEngineService,
  ) {}

  onModuleInit() {
    this.subscriber.subscribe(STRATEGY_REGENERATE_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${STRATEGY_REGENERATE_CHANNEL}: ${err.message}`);
      } else {
        this.logger.log(`Listening for supervisor regeneration triggers on '${STRATEGY_REGENERATE_CHANNEL}'.`);
      }
    });

    this.subscriber.on('message', (channel, message) => {
      if (channel !== STRATEGY_REGENERATE_CHANNEL) return;
      let payload: { tickerId?: string; reason?: string };
      try {
        payload = JSON.parse(message);
      } catch (e) {
        this.logger.error(`Bad ${STRATEGY_REGENERATE_CHANNEL} payload: ${e.message}`);
        return;
      }
      const { tickerId, reason } = payload;
      if (!tickerId) {
        this.logger.warn(`Regeneration trigger missing tickerId; ignoring.`);
        return;
      }
      if (this.inFlight.has(tickerId)) {
        this.logger.log(`Regeneration already in flight for ticker ${tickerId}; skipping duplicate trigger.`);
        return;
      }

      this.inFlight.add(tickerId);
      this.strategyEngine
        .generateStrategyForTicker(tickerId, reason || 'supervisor guardrail trip')
        .then(() => this.logger.log(`Regeneration complete for ticker ${tickerId}.`))
        .catch((err) => this.logger.error(`Regeneration failed for ticker ${tickerId}: ${err.message}`))
        .finally(() => this.inFlight.delete(tickerId));
    });
  }
}
