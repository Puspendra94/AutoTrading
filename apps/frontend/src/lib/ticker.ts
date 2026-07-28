// Single-symbol helper. The whole product is BTCUSDT-on-Binance only, so instead of
// a ticker picker every page resolves the one BTCUSDT ticker through here.
import { apiGet, apiPost } from './api';

export const SYMBOL = 'BTCUSDT';

export type Ticker = {
  id: string;
  symbol: string;
  interval: string;
  status: string;
  onboardingStage: string;
  provider?: { id: string; name: string; type: string };
  // 'spot' | 'futures' — futures tickers can hold shorts and route live orders to the futures venue.
  marketType?: { name: string } | null;
};

// We store/stream BTCUSDT at 1m (the minimum interval) as the single source of truth;
// every higher interval is a DB-level roll-up. So the canonical ticker is the 1m one.
export const BASE_INTERVAL = '1m';

/** Returns the canonical BTCUSDT ticker — preferring the 1m, non-inactive one — or null. */
export async function resolveBtcTicker(): Promise<Ticker | null> {
  const tickers = await apiGet<Ticker[]>('/market-data/tickers');
  const btc = tickers.filter((t) => t.symbol === SYMBOL);
  if (!btc.length) return null;
  const live = btc.filter((t) => t.status !== 'inactive');
  const pool = live.length ? live : btc;
  return pool.find((t) => t.interval === BASE_INTERVAL) ?? pool[0];
}

/**
 * Resolve the BTCUSDT ticker, creating it against the given Binance provider if none
 * exists yet (kicks off the backend's async backfill + live-stream startup).
 */
export async function ensureBtcTicker(providerId: string, interval = '1m'): Promise<Ticker> {
  const existing = await resolveBtcTicker();
  if (existing) return existing;
  return apiPost<Ticker>('/market-data/tickers', { providerId, symbol: SYMBOL, interval });
}
