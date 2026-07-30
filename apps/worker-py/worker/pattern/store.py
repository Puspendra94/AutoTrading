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


def _bars_between(opened_at, closed_at, bar_ms: int = 900_000) -> int:
    """Approximate bars held, for the failure summary. Approximate is fine — it is context for a
    prompt, not an accounting figure."""
    if not opened_at or not closed_at:
        return 0
    return max(int((closed_at - opened_at).total_seconds() * 1000 / bar_ms), 0)


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

    async def save_decision(self, decision: dict) -> None:
        """Persist one bar's decision. Upsert on (ticker, interval, bar) so re-processing a bar
        after a restart corrects the row rather than duplicating it.

        Every jsonb value is passed as a dict — db.py registers an encoder=json.dumps codec, so
        pre-serialising here would store a JSON string inside the column.
        """
        bar_time = datetime.fromtimestamp(decision["barTimeMs"] / 1000.0, tz=timezone.utc)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pattern_decisions
                    (ticker_id, interval, bar_time, outcome, reason, gate, state_pack,
                     llm_decision, validation, sizing, position_id, stop_price, take_profit,
                     model, cost_usd, input_tokens, output_tokens)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (ticker_id, interval, bar_time) DO UPDATE SET
                    outcome = EXCLUDED.outcome, reason = EXCLUDED.reason, gate = EXCLUDED.gate,
                    state_pack = EXCLUDED.state_pack, llm_decision = EXCLUDED.llm_decision,
                    validation = EXCLUDED.validation, sizing = EXCLUDED.sizing,
                    position_id = EXCLUDED.position_id, stop_price = EXCLUDED.stop_price,
                    take_profit = EXCLUDED.take_profit, model = EXCLUDED.model,
                    cost_usd = EXCLUDED.cost_usd, input_tokens = EXCLUDED.input_tokens,
                    output_tokens = EXCLUDED.output_tokens
                """,
                decision["tickerId"], decision["interval"], bar_time,
                decision["outcome"], decision.get("reason") or "",
                decision.get("gate"), decision.get("statePack"), decision.get("llmDecision"),
                decision.get("validation"), decision.get("sizing"),
                decision.get("positionId"), decision.get("stopPrice"), decision.get("takeProfit"),
                decision.get("model"), float(decision.get("costUsd") or 0.0),
                int(decision.get("inputTokens") or 0), int(decision.get("outputTokens") or 0),
            )

    async def set_protective_levels(self, position_id: str, *, stop_loss: float,
                                    take_profit: float, entry_price: float) -> None:
        """Stamp the exit ladder's starting state onto a freshly opened position.

        `initial_stop` is recorded separately from `stop_loss` and never changes: the live stop
        ratchets, so measuring a result in R against it would say a runner that trailed to +3R
        made 0R. `extreme_price` starts at entry.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE positions
                   SET stop_loss = $2, take_profit = $3, initial_stop = $2,
                       extreme_price = $4, lifecycle = 'open'
                   WHERE id = $1""",
                position_id, stop_loss, take_profit, entry_price,
            )

    async def ratchet_stop(self, position_id: str, *, new_stop: float, lifecycle: str,
                           extreme_price: float) -> None:
        """Move a stop toward profit. The WHERE clause enforces ratchet-only at the database
        level as well as in exits.py — two concurrent ticks must not be able to interleave and
        leave the looser of two stops behind."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE positions
                   SET stop_loss = $2, lifecycle = $3, extreme_price = $4
                   WHERE id = $1
                     AND (stop_loss IS NULL
                          OR (side = 'long'  AND $2 > stop_loss)
                          OR (side = 'short' AND $2 < stop_loss)
                          OR lifecycle <> $3)""",
                position_id, new_stop, lifecycle, extreme_price,
            )

    async def update_extreme(self, position_id: str, side: str, price: float) -> None:
        """Track the best price seen since entry — what the trailing stop measures from."""
        comparison = "GREATEST" if side == "long" else "LEAST"
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"""UPDATE positions
                    SET extreme_price = {comparison}(COALESCE(extreme_price, $2), $2)
                    WHERE id = $1""",
                position_id, price,
            )

    async def record_exit_reason(self, position_id: str, reason: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE positions SET exit_reason = $2 WHERE id = $1",
                               position_id, reason[:255])

    async def open_pattern_positions(self, ticker_id: str) -> list[dict]:
        """Open positions owned by THIS brain, with their ladder state.

        `strategy_id IS NULL` is the ownership test: the strategy engine always stamps one, the
        pattern brain never does. Without it this loop would manage the other engine's trades.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, side, entry_price, quantity, stop_loss, take_profit, initial_stop,
                          extreme_price, lifecycle, opened_at
                   FROM positions
                   WHERE ticker_id = $1 AND status = 'open' AND strategy_id IS NULL""",
                ticker_id,
            )
        return [{
            "id": str(r["id"]), "side": r["side"], "entryPrice": r["entry_price"],
            "quantity": r["quantity"], "stopLoss": r["stop_loss"], "takeProfit": r["take_profit"],
            "initialStop": r["initial_stop"], "extremePrice": r["extreme_price"],
            "lifecycle": r["lifecycle"] or "open", "openedAt": r["opened_at"],
        } for r in rows]

    async def recent_failures(self, ticker_id: str, limit: int = 5) -> list[dict]:
        """The last few LOSING trades, compressed to what the prompt can use.

        Deliberately short. The operator's concern was that a couple of unlucky trades in one
        setup would teach the model to avoid that setup permanently, so it sees a handful of
        recent facts rather than an accumulating case against every pattern.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT p.side, p.realized_pl, p.entry_price, p.opened_at, p.closed_at,
                       d.reason, d.gate, d.state_pack -> 'regime' ->> 'label' AS regime,
                       d.sizing ->> 'riskUsd' AS risk_usd
                FROM positions p
                LEFT JOIN pattern_decisions d ON d.position_id = p.id
                WHERE p.ticker_id = $1 AND p.status = 'closed' AND p.realized_pl < 0
                  AND p.strategy_id IS NULL
                ORDER BY p.closed_at DESC NULLS LAST
                LIMIT $2
                """,
                ticker_id, limit,
            )

        failures = []
        for r in rows:
            risk = float(r["risk_usd"]) if r["risk_usd"] else 0.0
            realized = float(r["realized_pl"] or 0.0)
            gate = r["gate"] or {}
            triggers = gate.get("triggers") if isinstance(gate, dict) else None
            failures.append({
                "side": r["side"],
                "regime": r["regime"] or "unknown",
                "trigger": (triggers or ["unknown"])[0],
                # Expressed in R (multiples of what the trade risked) rather than dollars, so the
                # number means the same thing across different account sizes.
                "rMultiple": (realized / risk) if risk > 0 else 0.0,
                "barsHeld": _bars_between(r["opened_at"], r["closed_at"]),
                "exitReason": r["reason"] or "unknown",
            })
        return failures

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
