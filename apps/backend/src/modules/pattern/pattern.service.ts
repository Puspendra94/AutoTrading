import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { PatternSignal } from '../../entities/pattern-signal.entity';

export interface PatternMarker {
  time: number; // seconds — lightweight-charts' UTCTimestamp
  kind: string;
  side: 'long' | 'short';
  price: number;
  detail: string;
}

const INTERVAL_MS: Record<string, number> = {
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  '1h': 3_600_000,
  '4h': 14_400_000,
  '1d': 86_400_000,
  '1w': 604_800_000,
};

/**
 * Read side of the pattern brain. The worker writes `pattern_signals`; this serves them to the
 * chart.
 *
 * Unlike the strategy brain's markers there is no Redis round-trip here — nothing needs to be
 * replayed or recomputed to answer, because the worker already stored the finished trigger. That
 * also means the chart keeps rendering markers when the worker is down.
 */
@Injectable()
export class PatternService {
  constructor(
    @InjectRepository(PatternSignal)
    private readonly signals: Repository<PatternSignal>,
  ) {}

  /**
   * Markers bucketed onto `displayInterval` bars. A trigger is stored once, at the interval the
   * engine evaluated (15m), so the same row has to land on the right bar whichever timeframe the
   * user is viewing — bucketing on read is what avoids duplicating every event per interval.
   */
  async getMarkers(tickerId: string, displayInterval: string, limit: number): Promise<PatternMarker[]> {
    const stepMs = INTERVAL_MS[displayInterval] ?? INTERVAL_MS['15m'];
    const rows = await this.signals.query(
      `
      SELECT DISTINCT ON (bar_bucket, kind) bar_bucket, kind, side, price, detail
      FROM (
        SELECT to_timestamp(
                 floor(extract(epoch FROM bar_time) * 1000 / $2::bigint) * $2::bigint / 1000.0
               ) AS bar_bucket,
               kind, side, price, detail, bar_time
        FROM "Algo_Trading"."pattern_signals"
        WHERE ticker_id = $1
        ORDER BY bar_time DESC
        LIMIT $3
      ) t
      ORDER BY bar_bucket ASC, kind, bar_time DESC
      `,
      [tickerId, stepMs, limit],
    );

    return rows.map((r: any) => ({
      time: Math.floor(new Date(r.bar_bucket).getTime() / 1000),
      kind: r.kind,
      side: r.side,
      price: Number(r.price),
      detail: r.detail,
    }));
  }

  /**
   * The most recent stored feature state — regime, levels, indicators — for the dashboard strip.
   * Null before the engine has ever produced a trigger for this ticker.
   */
  async getLatestState(tickerId: string): Promise<Record<string, unknown> | null> {
    // Explicitly skip rows without a pack rather than taking the newest row and hoping: a
    // pack-less row would otherwise shadow a perfectly good older state.
    const rows = await this.signals.query(
      `SELECT state_pack FROM "Algo_Trading"."pattern_signals"
       WHERE ticker_id = $1 AND state_pack IS NOT NULL
       ORDER BY bar_time DESC LIMIT 1`,
      [tickerId],
    );
    return rows[0]?.state_pack ?? null;
  }
}
