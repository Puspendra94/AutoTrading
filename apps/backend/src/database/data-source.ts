import { DataSource, DataSourceOptions } from 'typeorm';
import * as path from 'path';
import { config } from '../config/configuration';

// `config` loads apps/backend/.env on import — no separate dotenv call needed here.
export const dataSourceOptions: DataSourceOptions = {
  type: 'postgres',
  host: config.database.host,
  port: config.database.port,
  username: config.database.user,
  password: config.database.password,
  database: config.database.name,
  migrationsTableName: 'migrations',
  entities: [path.join(__dirname, '../entities/*.entity{.ts,.js}')],
  migrations: [path.join(__dirname, '../migrations/*{.ts,.js}')],
  synchronize: false,
  logging: config.database.logging,
};

const dataSource = new DataSource(dataSourceOptions);
export default dataSource;
