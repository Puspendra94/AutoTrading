import { NestFactory } from '@nestjs/core';
import { ValidationPipe } from '@nestjs/common';
import { AppModule } from './app.module';
// `config` loads apps/backend/.env on import (see config/configuration.ts).
import { config } from './config/configuration';
import migrationDataSource from './database/data-source';

async function bootstrap() {
  // Run schema migrations BEFORE the Nest app initializes, so the schema exists before any
  // module's onModuleInit touches the DB (e.g. the market-data seed queries `tickers`). Uses a
  // dedicated short-lived connection, closed again before the app opens its own pool. Fails
  // hard on error — a half-migrated schema must stop startup, not boot into a broken app.
  if (!migrationDataSource.isInitialized) {
    await migrationDataSource.initialize();
  }
  await migrationDataSource.runMigrations();
  await migrationDataSource.destroy();
  console.log('Database migrations completed.');

  const app = await NestFactory.create(AppModule);

  app.enableCors({
    origin: '*',
    credentials: true,
  });

  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      transform: true,
    }),
  );

  await app.listen(config.port);
  console.log(`Algo Trading Backend running on http://localhost:${config.port}`);
}

bootstrap();
