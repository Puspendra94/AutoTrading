import * as dotenv from 'dotenv';
import * as path from 'path';

// Same .env loading as main.ts — must run before WorkerModule (and its @Cron/@Interval
// decorators, which read process.env at class-definition time) is imported.
dotenv.config({ path: path.resolve(__dirname, '../.env') });
dotenv.config();

import { NestFactory } from '@nestjs/core';
import { WorkerModule } from './worker.module';

async function bootstrap() {
  // No HTTP listener, no WebSocket gateway — this process only runs the scheduled
  // jobs in JobsModule (spec Section 3/14.2: background jobs run in a separate process
  // from the live order-execution path).
  const app = await NestFactory.createApplicationContext(WorkerModule);
  console.log('Algo Trading worker started — scheduled jobs active (balance sync, reconciliation, strategy re-evaluation, allocation rebalance, data quality monitor, alert digest).');

  process.on('SIGTERM', async () => {
    await app.close();
    process.exit(0);
  });
  process.on('SIGINT', async () => {
    await app.close();
    process.exit(0);
  });
}

bootstrap();
