import { Inject, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import Redis from 'ioredis';
import { REDIS_SUBSCRIBER } from '../common/redis/redis.module';
import { TradingGateway } from './trading.gateway';

export const STRATEGY_GENERATED_CHANNEL = 'strategy:generated';

/**
 * Consolidation Phase A — async generation UX. When GENERATION_SOURCE=worker, the worker runs
 * strategy generation in the background and publishes the result to Redis. This bridge (API
 * process only) forwards it to connected browsers over Socket.IO so the Generate modal can
 * resolve without a long blocking HTTP request. Mirrors RedisAlertsBridgeService.
 */
@Injectable()
export class RedisGenerationBridgeService implements OnModuleInit {
  private readonly logger = new Logger(RedisGenerationBridgeService.name);

  constructor(
    @Inject(REDIS_SUBSCRIBER) private readonly subscriber: Redis,
    private readonly tradingGateway: TradingGateway,
  ) {}

  onModuleInit() {
    this.subscriber.subscribe(STRATEGY_GENERATED_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${STRATEGY_GENERATED_CHANNEL}: ${err.message}`);
      }
    });

    this.subscriber.on('message', (channel, message) => {
      if (channel !== STRATEGY_GENERATED_CHANNEL) return;
      try {
        const result = JSON.parse(message);
        this.logger.log(`Generation result for request ${result.requestId} (saved=${result.saved}); forwarding to clients.`);
        this.tradingGateway.broadcastGenerationComplete(result);
      } catch (e) {
        this.logger.error(`Failed to parse generation result: ${e.message}`);
      }
    });
  }
}
