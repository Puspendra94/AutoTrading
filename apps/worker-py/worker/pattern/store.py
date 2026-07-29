"""Persistence for the pattern brain — asyncpg over `pattern_signals`.

Triggers are written at the interval the feature engine evaluates (config.pattern_interval),
once each. The read path buckets `bar_time` down to whatever timeframe the chart is displaying,
so a 15m trigger still lands on the right bar when the user is looking at 1h — without storing
the same event once per display interval.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("worker.pattern.store")

_INTERVAL_RE = re.compile(r"^(\d+)([mhdw])$", re.IGNORECASE)
_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def interval_ms(interval: str) -> int:
    m = _INTERVAL_RE.match(interval or "")
    if not m:
        raise ValueError(f"Unsupported interval: {interval}")
    return int(m.group(1)) * _UNIT_MS[m.group(2).lower()]


class PatternSignalStore:
    def __init__(self, pool) -> None:
        self.pool = pool

    async def save_triggers(
        self, ticker_id: str, interval: str, bar_time_ms: int,
        triggers: list[dict], state_pack: dict | None = None,
    ) -> int:
        """Persist this bar's triggers. Idempotent on (ticker, interval, bar, kind), so
        re-processing a bar — a reconnect replaying a close, a restart — inserts nothing new.

        Returns the number of rows actually inserted.
        """
        if not triggers:
            return 0

        bar_time = datetime.fromtimestamp(bar_time_ms / 1000.0, tz=timezone.utc)
        # Pass the dict straight through: db.py registers a jsonb codec with encoder=json.dumps,
        # so serialising here too stored a JSON *string* inside the jsonb column (jsonb_typeof =
        # 'string'). Readers then got text where they expected an object and every field came
        # back undefined.
        rows = [
            (ticker_id, interval, bar_time, t["name"], t["side"], float(t["price"]),
             str(t.get("detail") or ""), state_pack)
            for t in triggers
        ]
        async with self.pool.acquire() as conn:
            result = await conn.executemany(
                """
                INSERT INTO pattern_signals
                    (ticker_id, interval, bar_time, kind, side, price, detail, state_pack)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (ticker_id, interval, bar_time, kind) DO NOTHING
                """,
                rows,
            )
        # executemany returns None on asyncpg; the caller only needs "how many did we offer".
        del result
        return len(rows)

    async def load_markers(self, ticker_id: str, display_interval: str, limit: int = 500) -> list[dict]:
        """Triggers as chart markers, bucketed onto `display_interval` bars, oldest first.

        Bucketing in SQL keeps it to one round trip and means the same stored row renders
        correctly at every timeframe the chart offers.
        """
        step_ms = interval_ms(display_interval)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (bar_bucket, kind)
                       bar_bucket, kind, side, price, detail
                FROM (
                  SELECT to_timestamp(
                           floor(extract(epoch FROM bar_time) * 1000 / $2::bigint)
                           * $2::bigint / 1000.0
                         ) AS bar_bucket,
                         kind, side, price, detail, bar_time
                  FROM pattern_signals
                  WHERE ticker_id = $1
                  ORDER BY bar_time DESC
                  LIMIT $3
                ) t
                ORDER BY bar_bucket ASC, kind, bar_time DESC
                """,
                ticker_id, step_ms, limit,
            )
        return [
            {
                "time": int(r["bar_bucket"].timestamp()),
                "kind": r["kind"],
                "side": r["side"],
                "price": float(r["price"]),
                "detail": r["detail"],
            }
            for r in rows
        ]

    async def latest_state_pack(self, ticker_id: str) -> dict | None:
        """The most recently stored feature state — what the UI's regime/indicator strip reads."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT state_pack FROM pattern_signals
                   WHERE ticker_id = $1 AND state_pack IS NOT NULL
                   ORDER BY bar_time DESC LIMIT 1""",
                ticker_id,
            )
        if not row or row["state_pack"] is None:
            return None
        pack = row["state_pack"]
        return json.loads(pack) if isinstance(pack, str) else pack
