import { Global, Module } from '@nestjs/common';
import Redis from 'ioredis';
import { config } from '../../config/configuration';

export const REDIS_PUBLISHER = 'REDIS_PUBLISHER';
export const REDIS_SUBSCRIBER = 'REDIS_SUBSCRIBER';

function createClient(): Redis {
  return new Redis({
    host: config.redis.host,
    port: config.redis.port,
    // undefined (not '') when unset: ioredis sends AUTH for any defined value, and an empty
    // password is rejected by a server that has no requirepass configured.
    password: config.redis.password || undefined,
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
