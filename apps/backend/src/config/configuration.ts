import * as dotenv from 'dotenv';
import * as path from 'path';

/**
 * Single, typed, validated source of truth for every environment variable the backend
 * reads. Nothing else in `src/` should touch `process.env` directly — import `config`
 * (or a specific section) from here instead. This kills the previous drift where the
 * same key was parsed in several places with *different* inline defaults (e.g. the Redis
 * port defaulted to 6379 in one file while the app actually runs on 55000).
 *
 * Implemented as an eagerly-built frozen singleton rather than an injectable Nest service
 * on purpose: config is read from non-DI contexts too — module-level `const`s, class-field
 * initializers, the RedisModule factory, and `database/data-source.ts` (which the TypeORM
 * CLI loads with no Nest container at all). A singleton works uniformly in all of them; an
 * injectable would not.
 */

// Load the backend service-level .env exactly once. Primary lookup is cwd/.env — the cwd is
// apps/backend in every real run path (`nest start`, `jest`, the `typeorm` CLI, and
// `npm --prefix apps/backend`). The __dirname-relative path is a fallback for odd launchers.
// dotenv never overwrites an already-set variable, so the order is harmless.
dotenv.config({ path: path.resolve(process.cwd(), '.env') });
dotenv.config({ path: path.resolve(__dirname, '../../.env') });

export type LiveStreamSource = 'internal' | 'redis';
export type LiveExecutionSource = 'backend' | 'worker';

export interface AppConfig {
  readonly nodeEnv: string;
  readonly isProduction: boolean;
  readonly port: number;
  readonly frontendUrl: string;

  readonly database: {
    readonly host: string;
    readonly port: number;
    readonly user: string;
    readonly password: string;
    readonly name: string;
    readonly logging: boolean;
  };

  readonly redis: {
    readonly host: string;
    readonly port: number;
  };

  readonly auth: {
    readonly jwtSecret: string;
    readonly jwtExpiration: string;
  };

  readonly internalApiKey: string;

  readonly liveStreamSource: LiveStreamSource;
  readonly liveExecutionSource: LiveExecutionSource;
  // Where strategy GENERATION runs. 'backend' (default) = the in-process synchronous path.
  // 'worker' = the API publishes a strategy:generate job to Redis, the worker generates, and the
  // result is pushed back to the browser over the websocket. Consolidation Phase A (async UX).
  readonly generationSource: 'backend' | 'worker';
  readonly historicalBackfillEnabled: boolean;
  // Phase 2 hybrid AI exit overlay: on a rules-mode strategy, when the deterministic ladder says
  // HOLD on a winning open position, consult the LLM on whether to book the profit early. Off by
  // default (one LLM call per in-profit candle). Mirrors the worker's HYBRID_EXIT_AI.
  readonly hybridExitAi: boolean;

  readonly paper: {
    // Simulated starting equity for paper (simulated-fill) trading, used to compute paper
    // P/L, ROI and available balance on the dashboard. Real (live) balances come from the exchange.
    readonly startingBalanceUsd: number;
  };

  // Trading costs, charged on paper fills as well as live ones. Must stay in step with the
  // worker's FUTURES_TAKER_FEE_PCT / SPOT_TAKER_FEE_PCT / PAPER_SLIPPAGE_PCT — the two engines
  // can both close a position, and they must not book different P/L for the same trade.
  readonly costs: {
    readonly futuresTakerFeePct: number;
    readonly spotTakerFeePct: number;
    readonly paperSlippagePct: number;
  };

  readonly strategy: {
    // Timeframe strategies are generated/backtested on. We only ingest 1m candles; higher
    // intervals are aggregated on the fly via TimescaleDB time_bucket. 1m trend-following is
    // noise/fee-dominated (no edge), so the default is a higher interval where a real edge
    // can show. Any '<n><m|h|d|w>' interval (e.g. '1h', '4h', '1d').
    readonly evalInterval: string;
    // How many aggregated bars of `evalInterval` to backtest over.
    readonly evalCandleLimit: number;
  };

  readonly risk: {
    readonly defaultDailyLossLimitPct: number;
    readonly defaultMaxConcurrentPositions: number;
    readonly defaultProbationSizePct: number;
    readonly defaultProbationTradesCount: number;
    readonly allocationDiversificationCapPct: number;
    readonly providerApiFailureThreshold: number;
  };

  readonly llm: {
    readonly models: string;
    readonly anthropicApiKey: string;
    readonly deepseekApiKey: string;
    readonly groqApiKey: string;
  };

  readonly aws: {
    readonly region: string;
    readonly accessKeyId: string;
    readonly secretAccessKey: string;
    readonly sessionToken: string;
    readonly bedrockApiKey: string;
  };

  readonly telegram: {
    readonly botToken: string;
    readonly chatId: string;
  };
}

type Env = Record<string, string | undefined>;

const str = (env: Env, key: string, def = ''): string => {
  const v = env[key];
  return v === undefined || v === '' ? def : v;
};

const int = (env: Env, key: string, def: number): number => {
  const raw = env[key];
  if (raw === undefined || raw === '') return def;
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) throw new Error(`Env ${key}='${raw}' is not a valid integer.`);
  return n;
};

const float = (env: Env, key: string, def: number): number => {
  const raw = env[key];
  if (raw === undefined || raw === '') return def;
  const n = parseFloat(raw);
  if (Number.isNaN(n)) throw new Error(`Env ${key}='${raw}' is not a valid number.`);
  return n;
};

/**
 * Build (and validate) the config from a given environment. Pure and side-effect-free so it
 * can be unit-tested with a fake env; the module-level `config` below binds it to process.env.
 */
export function loadConfig(env: Env = process.env): AppConfig {
  // Required secrets — fail fast rather than falling back to an insecure baked-in default
  // (the old code shipped a hardcoded dev JWT secret, which is exactly what we want to avoid).
  const missing = (['JWT_SECRET', 'INTERNAL_API_KEY'] as const).filter((k) => !str(env, k));
  if (missing.length > 0) {
    throw new Error(
      `Missing required environment variable(s): ${missing.join(', ')}. ` +
        `Set them in apps/backend/.env (see apps/backend/.env.example).`,
    );
  }

  const nodeEnv = str(env, 'NODE_ENV', 'development');

  return Object.freeze({
    nodeEnv,
    isProduction: nodeEnv === 'production',
    port: int(env, 'PORT', 3009),
    frontendUrl: str(env, 'FRONTEND_URL', 'http://localhost:4321'),

    database: Object.freeze({
      host: str(env, 'DATABASE_HOST', 'localhost'),
      port: int(env, 'DATABASE_PORT', 5433),
      user: str(env, 'DATABASE_USER', 'postgres'),
      password: str(env, 'DATABASE_PASSWORD', 'postgres'),
      name: str(env, 'DATABASE_NAME', 'postgres'),
      // Off by default — TypeORM query logging drowns the API logs in insert spam from the
      // data pipeline. Set TYPEORM_LOGGING=true to turn it on when debugging SQL.
      logging: str(env, 'TYPEORM_LOGGING') === 'true',
    }),

    redis: Object.freeze({
      host: str(env, 'REDIS_HOST', 'localhost'),
      port: int(env, 'REDIS_PORT', 55000),
    }),

    auth: Object.freeze({
      jwtSecret: str(env, 'JWT_SECRET'),
      jwtExpiration: str(env, 'JWT_EXPIRATION', '7d'),
    }),

    internalApiKey: str(env, 'INTERNAL_API_KEY'),

    liveStreamSource: str(env, 'LIVE_STREAM_SOURCE') === 'redis' ? 'redis' : 'internal',
    liveExecutionSource: str(env, 'LIVE_EXECUTION_SOURCE') === 'worker' ? 'worker' : 'backend',
    generationSource: str(env, 'GENERATION_SOURCE') === 'worker' ? 'worker' : 'backend',
    // Enabled unless explicitly turned off.
    historicalBackfillEnabled: str(env, 'HISTORICAL_BACKFILL_ENABLED') !== 'false',
    hybridExitAi: str(env, 'HYBRID_EXIT_AI') === 'true',

    paper: Object.freeze({
      startingBalanceUsd: float(env, 'PAPER_STARTING_BALANCE_USD', 10000),
    }),

    costs: Object.freeze({
      futuresTakerFeePct: float(env, 'FUTURES_TAKER_FEE_PCT', 0.0004),
      spotTakerFeePct: float(env, 'SPOT_TAKER_FEE_PCT', 0.001),
      paperSlippagePct: float(env, 'PAPER_SLIPPAGE_PCT', 0.0002),
    }),

    strategy: Object.freeze({
      // 1h gives ~4x more trades than 4h, so the out-of-sample Sharpe is far less noisy and the
      // trade-count gate is reachable — the AI's own failure lessons kept asking for this.
      // Configurable: set STRATEGY_EVAL_INTERVAL to any '<n><m|h|d|w>' (e.g. 1h, 2h, 4h, 1d).
      evalInterval: str(env, 'STRATEGY_EVAL_INTERVAL', '1h'),
      // ~14 months of 1h bars: enough out-of-sample trades for a meaningful gate while staying
      // in a recent-enough regime (over ~2y+, no EMA-crossover config holds up — the edge is
      // regime-dependent, not durable).
      evalCandleLimit: int(env, 'STRATEGY_EVAL_CANDLE_LIMIT', 10000),
    }),

    risk: Object.freeze({
      defaultDailyLossLimitPct: float(env, 'DEFAULT_DAILY_LOSS_LIMIT_PCT', 2.0),
      defaultMaxConcurrentPositions: int(env, 'DEFAULT_MAX_CONCURRENT_POSITIONS', 1),
      defaultProbationSizePct: float(env, 'DEFAULT_PROBATION_SIZE_PCT', 25.0),
      defaultProbationTradesCount: int(env, 'DEFAULT_PROBATION_TRADES_COUNT', 10),
      allocationDiversificationCapPct: float(env, 'ALLOCATION_DIVERSIFICATION_CAP_PCT', 40),
      providerApiFailureThreshold: int(env, 'PROVIDER_API_FAILURE_THRESHOLD', 5),
    }),

    llm: Object.freeze({
      models: str(env, 'LLM_MODELS', 'direct_api:claude-opus-4-8'),
      anthropicApiKey: str(env, 'ANTHROPIC_API_KEY'),
      deepseekApiKey: str(env, 'DEEPSEEK_API_KEY'),
      groqApiKey: str(env, 'GROQ_API_KEY'),
    }),

    aws: Object.freeze({
      region: str(env, 'AWS_REGION'),
      accessKeyId: str(env, 'AWS_ACCESS_KEY_ID'),
      secretAccessKey: str(env, 'AWS_SECRET_ACCESS_KEY'),
      sessionToken: str(env, 'AWS_SESSION_TOKEN'),
      bedrockApiKey: str(env, 'AWS_BEDROCK_API_KEY'),
    }),

    telegram: Object.freeze({
      botToken: str(env, 'TELEGRAM_BOT_TOKEN'),
      chatId: str(env, 'TELEGRAM_CHAT_ID'),
    }),
  });
}

/** The bound singleton every module imports. Reading it validates required keys once, at load. */
export const config: AppConfig = loadConfig();
