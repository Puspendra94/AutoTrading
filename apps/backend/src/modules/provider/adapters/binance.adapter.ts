import { Injectable, Logger } from '@nestjs/common';
// Official Binance Node.js connector (spec 4.2.1) — HMAC apiKey/apiSecret auth,
// used instead of hand-rolled HTTP for correct request signing and rate-limit handling.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { Spot, WebsocketStream } = require('@binance/connector');

export interface BinanceCredentials {
  apiKey: string;
  apiSecret: string;
}

export interface RawCandle {
  timestamp: Date;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface LiveKlineTick {
  symbol: string;
  interval: string;
  openTime: number;
  closeTime: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  isFinal: boolean;
}

const MAINNET_REST = 'https://api.binance.com';
const TESTNET_REST = 'https://testnet.binance.vision';
const MAINNET_WS = 'wss://stream.binance.com:9443';
const TESTNET_WS = 'wss://stream.testnet.binance.vision';

/**
 * Thin wrapper around @binance/connector. Public market-data methods never require
 * credentials (Binance's klines/WS streams are unauthenticated). Account/order/balance
 * methods always require real credentials — there is no synthetic fallback here; a
 * failure surfaces to the caller rather than being papered over.
 */
@Injectable()
export class BinanceAdapter {
  private readonly logger = new Logger(BinanceAdapter.name);

  private restBaseUrl(useTestnet: boolean): string {
    return useTestnet ? TESTNET_REST : MAINNET_REST;
  }

  private wsBaseUrl(useTestnet: boolean): string {
    return useTestnet ? TESTNET_WS : MAINNET_WS;
  }

  private client(creds?: BinanceCredentials, useTestnet = false) {
    return new Spot(creds?.apiKey || '', creds?.apiSecret || '', {
      baseURL: this.restBaseUrl(useTestnet),
    });
  }

  /** Public klines — no credentials required. Always reads from mainnet: real market
   * data should reflect the real market regardless of whether the connected account
   * is paper/live or testnet/mainnet. */
  async getPublicKlines(symbol: string, interval: string, limit = 500): Promise<RawCandle[]> {
    const client = this.client(undefined, false);
    const res = await client.klines(symbol, interval, { limit });
    return (res.data as any[]).map((raw) => ({
      timestamp: new Date(raw[0]),
      open: parseFloat(raw[1]),
      high: parseFloat(raw[2]),
      low: parseFloat(raw[3]),
      close: parseFloat(raw[4]),
      volume: parseFloat(raw[5]),
    }));
  }

  async getAccountBalance(
    creds: BinanceCredentials,
    useTestnet: boolean,
  ): Promise<{ asset: string; free: number; locked: number }[]> {
    const client = this.client(creds, useTestnet);
    const res = await client.account();
    return (res.data.balances as any[])
      .map((b) => ({ asset: b.asset, free: parseFloat(b.free), locked: parseFloat(b.locked) }))
      .filter((b) => b.free > 0 || b.locked > 0);
  }

  async placeMarketOrder(
    creds: BinanceCredentials,
    useTestnet: boolean,
    symbol: string,
    side: 'BUY' | 'SELL',
    quantity: number,
  ): Promise<{ orderId: string; fillPrice: number; fillQuantity: number; status: string; raw: any }> {
    const client = this.client(creds, useTestnet);
    const res = await client.newOrder(symbol, side, 'MARKET', {
      quantity: quantity.toFixed(8),
      newOrderRespType: 'FULL',
    });
    const data = res.data;
    const fills: any[] = data.fills || [];
    const totalQty = fills.reduce((s, f) => s + parseFloat(f.qty), 0) || parseFloat(data.executedQty || '0');
    const avgPrice =
      fills.length > 0
        ? fills.reduce((s, f) => s + parseFloat(f.price) * parseFloat(f.qty), 0) / totalQty
        : parseFloat(data.price || '0');
    return {
      orderId: String(data.orderId),
      fillPrice: avgPrice,
      fillQuantity: totalQty,
      status: data.status,
      raw: data,
    };
  }

  async getOpenPositionsAndBalance(
    creds: BinanceCredentials,
    useTestnet: boolean,
  ): Promise<{ balances: { asset: string; free: number; locked: number }[]; openOrders: any[] }> {
    const client = this.client(creds, useTestnet);
    const [account, openOrders] = await Promise.all([client.account(), client.openOrders()]);
    return {
      balances: (account.data.balances as any[])
        .map((b: any) => ({ asset: b.asset, free: parseFloat(b.free), locked: parseFloat(b.locked) }))
        .filter((b) => b.free > 0 || b.locked > 0),
      openOrders: openOrders.data,
    };
  }

  /**
   * Live kline WebSocket stream — public, no credentials needed. The connector handles
   * reconnection internally (spec 4.2.1). Returns the stream handle so the caller can
   * `.disconnect()` it when the ticker is deactivated.
   */
  streamKlines(
    symbol: string,
    interval: string,
    onCandle: (tick: LiveKlineTick) => void,
    onError?: (err: Error) => void,
  ) {
    const stream = new WebsocketStream({
      wsURL: this.wsBaseUrl(false),
      reconnectDelay: 5000,
      callbacks: {
        open: () => this.logger.log(`Kline stream opened: ${symbol}@${interval}`),
        close: () => this.logger.warn(`Kline stream closed: ${symbol}@${interval}`),
        error: (err: any) => {
          this.logger.error(`Kline stream error for ${symbol}@${interval}: ${err?.message || err}`);
          onError?.(err instanceof Error ? err : new Error(String(err)));
        },
        message: (raw: string) => {
          try {
            const msg = JSON.parse(raw);
            const k = msg.k;
            if (!k) return;
            onCandle({
              symbol: msg.s,
              interval: k.i,
              openTime: k.t,
              closeTime: k.T,
              open: parseFloat(k.o),
              high: parseFloat(k.h),
              low: parseFloat(k.l),
              close: parseFloat(k.c),
              volume: parseFloat(k.v),
              isFinal: k.x,
            });
          } catch (e) {
            this.logger.error(`Failed to parse kline message for ${symbol}: ${e.message}`);
          }
        },
      },
    });
    stream.kline(symbol.toLowerCase(), interval);
    return stream;
  }
}
