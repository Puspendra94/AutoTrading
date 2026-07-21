import * as dotenv from 'dotenv';
import * as path from 'path';

// Load backend service-level .env BEFORE application bootstrap
dotenv.config({ path: path.resolve(__dirname, '../.env') });
dotenv.config();

import { NestFactory } from '@nestjs/core';
import { ValidationPipe } from '@nestjs/common';
import { AppModule } from './app.module';

async function bootstrap() {
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

  const port = process.env.PORT ? parseInt(process.env.PORT, 10) : 3009;
  await app.listen(port);
  console.log(`Algo Trading Backend running on http://localhost:${port}`);
}

bootstrap();
