import { Injectable } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { PatternSignal } from '../../entities/pattern-signal.entity';
import { PatternDecision } from '../../entities/pattern-decision.entity';

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
    @InjectRepository(PatternDecision)
    private readonly decisions: Repository<PatternDecision>,
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
  /**
   * The decision feed. Free "no trigger" skips are excluded by default because on 15m they are
   * the overwhelming majority (~90 of 96 bars a day) and would bury the decisions that matter;
   * pass includeSkips to see the full record.
   */
  async getDecisions(tickerId: string, limit: number, includeSkips: boolean) {
    const qb = this.decisions
      .createQueryBuilder('d')
      .where('d.ticker_id = :tickerId', { tickerId })
      .orderBy('d.bar_time', 'DESC')
      .limit(limit);

    if (!includeSkips) {
      qb.andWhere('d.outcome != :skipped', { skipped: 'skipped_no_trigger' });
    }

    const rows = await qb.getMany();
    return rows.map((d) => ({
      id: d.id,
      barTime: Math.floor(new Date(d.barTime).getTime() / 1000),
      interval: d.interval,
      outcome: d.outcome,
      reason: d.reason,
      triggers: (d.gate as any)?.triggers ?? [],
      // The model's proposal exactly as it came back, including on rejected decisions.
      llmDecision: d.llmDecision,
      sizing: d.sizing,
      positionId: d.positionId,
      stopPrice: d.stopPrice === null ? null : Number(d.stopPrice),
      takeProfit: d.takeProfit === null ? null : Number(d.takeProfit),
      model: d.model,
      costUsd: Number(d.costUsd ?? 0),
      tokens: (d.inputTokens ?? 0) + (d.outputTokens ?? 0),
    }));
  }

  /** Counts by outcome plus total spend — the "is the gate too tight?" answer at a glance. */
  async getDecisionSummary(tickerId: string, sinceHours: number) {
    const rows = await this.decisions.query(
      `SELECT outcome, count(*)::int AS count, COALESCE(sum(cost_usd), 0)::float AS cost,
              COALESCE(sum(input_tokens + output_tokens), 0)::int AS tokens
       FROM "Algo_Trading"."pattern_decisions"
       WHERE ticker_id = $1 AND bar_time >= now() - ($2 || ' hours')::interval
       GROUP BY outcome`,
      [tickerId, String(sinceHours)],
    );
    const byOutcome: Record<string, number> = {};
    let totalCost = 0;
    let totalTokens = 0;
    let evaluated = 0;
    for (const r of rows) {
      byOutcome[r.outcome] = r.count;
      totalCost += r.cost;
      totalTokens += r.tokens;
      evaluated += r.count;
    }
    return { sinceHours, evaluated, byOutcome, totalCostUsd: totalCost, totalTokens };
  }

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
