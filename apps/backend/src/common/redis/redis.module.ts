import { Global, Module } from '@nestjs/common';
import Redis from 'ioredis';

export const REDIS_PUBLISHER = 'REDIS_PUBLISHER';
export const REDIS_SUBSCRIBER = 'REDIS_SUBSCRIBER';

function createClient(): Redis {
  return new Redis({
    host: process.env.REDIS_HOST || 'localhost',
    port: process.env.REDIS_PORT ? parseInt(process.env.REDIS_PORT, 10) : 6379,
    lazyConnect: false,
    retryStrategy: (times) => Math.min(times * 200, 5000),
  });
}

// Two distinct connections: ioredis clients that call `.subscribe()` can no longer
// issue other commands, so publishing and subscribing need separate connections.
@Global()
@Module({
  providers: [
    { provide: REDIS_PUBLISHER, useFactory: createClient },
    { provide: REDIS_SUBSCRIBER, useFactory: createClient },
  ],
  exports: [REDIS_PUBLISHER, REDIS_SUBSCRIBER],
})
export class RedisModule {}
