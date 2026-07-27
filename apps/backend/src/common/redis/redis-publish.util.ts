import type { Logger } from '@nestjs/common';
import type Redis from 'ioredis';

/**
 * Publish a JSON payload to a Redis channel without letting a pub/sub failure bubble into
 * (or block) the caller's main flow — publishing is always best-effort cross-process delivery
 * layered on top of the DB write of record. Centralized here so every publisher serializes and
 * error-handles the same way.
 */
export function publishJson(
  redis: Redis,
  channel: string,
  payload: unknown,
  logger?: Logger,
): void {
  redis.publish(channel, JSON.stringify(payload)).catch((err) =>
    logger?.warn(`Failed to publish to Redis channel '${channel}': ${err.message}`),
  );
}
