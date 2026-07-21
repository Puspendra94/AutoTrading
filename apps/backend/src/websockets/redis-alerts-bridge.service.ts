import { Inject, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import Redis from 'ioredis';
import { REDIS_SUBSCRIBER } from '../common/redis/redis.module';
import { TradingGateway } from './trading.gateway';

export const ALERTS_CHANNEL = 'alerts:created';

/**
 * Alerts can be created from the worker process (reconciliation, daily re-evaluation,
 * allocation) which has no Socket.IO server of its own. NotificationService publishes
 * every created alert to Redis; this bridge (API process only) subscribes and forwards
 * to connected browser clients via TradingGateway — this is the cross-process pub/sub
 * layer spec Section 2.8 calls for, applied to alert delivery.
 */
@Injectable()
export class RedisAlertsBridgeService implements OnModuleInit {
  private readonly logger = new Logger(RedisAlertsBridgeService.name);

  constructor(
    @Inject(REDIS_SUBSCRIBER) private readonly subscriber: Redis,
    private readonly tradingGateway: TradingGateway,
  ) {}

  onModuleInit() {
    this.subscriber.subscribe(ALERTS_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${ALERTS_CHANNEL}: ${err.message}`);
      }
    });

    this.subscriber.on('message', (channel, message) => {
      if (channel !== ALERTS_CHANNEL) return;
      try {
        const alert = JSON.parse(message);
        this.tradingGateway.broadcastAlert(alert);
      } catch (e) {
        this.logger.error(`Failed to parse alert message: ${e.message}`);
      }
    });
  }
}
