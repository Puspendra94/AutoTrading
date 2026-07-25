import { Inject, Injectable, Logger, OnModuleInit } from '@nestjs/common';
import Redis from 'ioredis';
import { REDIS_SUBSCRIBER } from '../../common/redis/redis.module';
import { InternalJobsService } from './internal-jobs.service';

// Must match worker-py's redis_bus.JOBS_TRIGGER_CHANNEL.
export const JOBS_TRIGGER_CHANNEL = 'jobs:trigger';

/**
 * Phase 2 (see MIGRATION.md): the Python worker used to POST /internal/jobs/* synchronously,
 * so it depended on the backend being reachable at trigger time. It now *publishes* a job
 * name to Redis instead; this consumer (API process) picks it up and runs the same proven
 * InternalJobsService body. Result: the worker and backend are decoupled — the worker never
 * blocks on or fails because of the backend, and the two deploy/restart independently.
 *
 * The guarded HTTP endpoints (InternalJobsController) stay for manual/debug triggers; the
 * scheduled path is this async channel.
 */
@Injectable()
export class InternalJobsRedisConsumer implements OnModuleInit {
  private readonly logger = new Logger(InternalJobsRedisConsumer.name);

  // Job name (matches the worker's payload) -> the service method that runs it.
  private readonly handlers: Record<string, () => Promise<any>>;

  constructor(
    @Inject(REDIS_SUBSCRIBER) private readonly subscriber: Redis,
    private readonly jobs: InternalJobsService,
  ) {
    this.handlers = {
      'balance-sync': () => this.jobs.balanceSync(),
      reconciliation: () => this.jobs.reconciliation(),
      'allocation-rebalance': () => this.jobs.allocationRebalance(),
      'alert-digest': () => this.jobs.alertDigest(),
      'strategy-reevaluation': () => this.jobs.strategyReevaluation(),
      'data-quality': () => this.jobs.dataQuality(),
    };
  }

  onModuleInit() {
    this.subscriber.subscribe(JOBS_TRIGGER_CHANNEL, (err) => {
      if (err) {
        this.logger.error(`Failed to subscribe to ${JOBS_TRIGGER_CHANNEL}: ${err.message}`);
      } else {
        this.logger.log(`Listening for async job triggers on '${JOBS_TRIGGER_CHANNEL}'.`);
      }
    });

    this.subscriber.on('message', (channel, message) => {
      if (channel !== JOBS_TRIGGER_CHANNEL) return;
      let job: string;
      try {
        job = JSON.parse(message).job;
      } catch (e) {
        this.logger.error(`Bad ${JOBS_TRIGGER_CHANNEL} payload: ${e.message}`);
        return;
      }
      const handler = this.handlers[job];
      if (!handler) {
        this.logger.warn(`Unknown job '${job}' on ${JOBS_TRIGGER_CHANNEL}; ignoring.`);
        return;
      }
      // Fire-and-forget: run the job, log the outcome. A failure here must never bubble
      // into the shared subscriber connection.
      handler()
        .then((result) => this.logger.log(`Job '${job}' complete: ${JSON.stringify(result)}`))
        .catch((err) => this.logger.error(`Job '${job}' failed: ${err.message}`));
    });
  }
}
