// Socket.IO singleton wrapping the trading gateway's real-time events.
// Server (apps/backend/src/websockets/trading.gateway.ts) currently accepts an
// unauthenticated connection; we pass the JWT in the `auth` payload anyway so it
// keeps working if/when the gateway starts validating it.
import { io, type Socket } from 'socket.io-client';
import { getToken } from './api';

let socket: Socket | null = null;

export function getSocket(): Socket {
  if (socket) return socket;
  // Default to same origin (empty url) so socket.io connects to the frontend, which
  // proxies `/socket.io` (HTTP + WS upgrade) through to the backend — same reasoning as
  // API_URL in api.ts. Override with PUBLIC_WS_URL to hit a backend directly.
  const url = (import.meta.env.PUBLIC_WS_URL as string) || '';
  socket = io(url, {
    auth: { token: getToken() },
    transports: ['websocket', 'polling'],
    autoConnect: true,
  });
  return socket;
}

export function subscribeTicker(tickerId: string) {
  if (!tickerId) return;
  getSocket().emit('subscribe_ticker', { tickerId });
}

export function unsubscribeTicker(tickerId: string) {
  if (!tickerId) return;
  getSocket().emit('unsubscribe_ticker', { tickerId });
}

export type PriceUpdatePayload = { tickerId: string; candle: any };

export function onPriceUpdate(cb: (data: PriceUpdatePayload) => void) {
  getSocket().on('price_update', cb);
  return () => getSocket().off('price_update', cb);
}

export function onPositionsUpdate(cb: (positions: any[]) => void) {
  getSocket().on('live_positions_update', cb);
  return () => getSocket().off('live_positions_update', cb);
}

// New event from the parallel backend effort — not guaranteed to exist yet.
// Wiring it up is harmless: if the server never emits it, the listener just never fires.
export function onAlertCreated(cb: (alert: any) => void) {
  getSocket().on('alert_created', cb);
  return () => getSocket().off('alert_created', cb);
}

export type GenerationResult = {
  requestId: string;
  tickerId?: string;
  saved: boolean;
  attempts?: number;
  message?: string;
  evaluation?: any;
  strategyId?: string | null;
  gateBypassed?: boolean;
};

// Async strategy generation (GENERATION_SOURCE=worker): the worker pushes the result here.
export function onGenerationComplete(cb: (result: GenerationResult) => void) {
  getSocket().on('strategy_generation_complete', cb);
  return () => getSocket().off('strategy_generation_complete', cb);
}
