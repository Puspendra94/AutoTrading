import { Inject, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import Redis from 'ioredis';
import { randomUUID } from 'crypto';
import { REDIS_PUBLISHER, REDIS_SUBSCRIBER } from '../../common/redis/redis.module';
import { publishJson } from '../../common/redis/redis-publish.util';

const SIGNALS_REQUEST_CHANNEL = 'signals:request';
const SIGNALS_RESPONSE_CHANNEL = 'signals:response';

export interface SignalsResult {
  strategyId: string | null;
  strategyName: string | null;
  direction: 'long' | 'short';
  signals: Array<{ time: number; side: 'buy' | 'sell'; price: number; reason: string }>;
}

const EMPTY: SignalsResult = { strategyId: null, strategyName: null, direction: 'long', signals: [] };

/**
 * Consolidation Phase B — chart markers are computed by the worker (which owns the DSL engine), not
 * the backend. This bridge does a synchronous-feeling request/response over Redis: publish a
 * signals:request and await the matching signals:response (by requestId), with a timeout so a slow
 * or absent worker degrades to empty markers rather than hanging the HTTP request.
 */
@Injectable()
export class SignalsBridgeService implements OnModuleInit {
  private readonly logger = new Logger(SignalsBridgeService.name);
  private readonly pending = new Map<string, (r: SignalsResult) => void>();

  constructor(
    @Inject(REDIS_PUBLISHER) private readonly publisher: Redis,
    @Inject(REDIS_SUBSCRIBER) private readonly subscriber: Redis,
  ) {}

  onModuleInit() {
    this.subscriber.subscribe(SIGNALS_RESPONSE_CHANNEL, (err) => {
      if (err) this.logger.error(`Failed to subscribe to ${SIGNALS_RESPONSE_CHANNEL}: ${err.message}`);
    });
    this.subscriber.on('message', (channel, message) => {
      if (channel !== SIGNALS_RESPONSE_CHANNEL) return;
      try {
        const r = JSON.parse(message);
        const resolve = this.pending.get(r.requestId);
        if (resolve) {
          this.pending.delete(r.requestId);
          resolve({ strategyId: r.strategyId ?? null, strategyName: r.strategyName ?? null,
            direction: r.direction === 'short' ? 'short' : 'long', signals: r.signals ?? [] });
        }
      } catch (e) {
        this.logger.error(`Failed to parse signals:response: ${e.message}`);
      }
    });
  }

  async requestSignals(tickerId: string, interval: string, limit: number): Promise<SignalsResult> {
    const requestId = randomUUID();
    const result = new Promise<SignalsResult>((resolve) => {
      const timer = setTimeout(() => {
        if (this.pending.delete(requestId)) {
          this.logger.warn(`signals:request ${requestId} timed out; returning empty markers.`);
          resolve(EMPTY);
        }
      }, 6000);
      this.pending.set(requestId, (r) => {
        clearTimeout(timer);
        resolve(r);
      });
    });
    publishJson(this.publisher, SIGNALS_REQUEST_CHANNEL, { requestId, tickerId, interval, limit }, this.logger);
    return result;
  }
}
