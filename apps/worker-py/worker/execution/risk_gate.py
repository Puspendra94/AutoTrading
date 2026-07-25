"""Deterministic Unbypassable Risk Gate — faithful port of risk-gate.service.ts (Phase 3c-1).

Every order source (Mode A rules, Mode B live AI, or a manual order) must pass through
evaluate_order_risk_gate before an order is placed. This is the money-safety layer, so it is
ported step-for-step and unit-tested against the same scenarios as the backend's
risk-gate.service.spec.ts.

This module makes NO trading decisions of its own beyond the backend's and places NO orders —
order execution is Phase 3c-2. DB reads and the breach side-effects (mark breached, alert,
flatten) are behind RiskGateStore so the decision logic is testable with a fake store.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from ..strategy.evaluator import to_fixed

DAY_ABBREVIATIONS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class RiskGateHalt(Exception):
    """Trading is halted (mirrors the backend's ForbiddenException) — the order is blocked
    AND a broader halt/flatten has been triggered."""


class RiskGateError(Exception):
    """Invalid input (mirrors BadRequestException) — bad ticker/provider."""


@dataclass
class OrderIntent:
    ticker_id: str
    side: str  # 'long' | 'short'
    price: float
    strategy_id: Optional[str] = None


def _num(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class RiskGateStore(Protocol):
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def get_provider(self, provider_id: str) -> Optional[dict]: ...
    async def get_schedule(self, provider_id: str) -> Optional[dict]: ...
    async def get_risk_limit(self, provider_id: str) -> Optional[dict]: ...
    async def get_today_tracker(self, provider_id: str, today: str) -> Optional[dict]: ...
    async def get_latest_balance(self, provider_id: str) -> Optional[dict]: ...
    async def create_today_tracker(self, provider_id: str, today: str, cum_base: float) -> None: ...
    async def mark_breached(self, provider_id: str, today: str) -> None: ...
    async def create_alert(self, severity: str, category: str, message: str, related_type: str, related_id: str) -> None: ...
    async def flatten_all_positions(self, provider_id: str, reason: str) -> None: ...
    async def count_open_positions(self, ticker_id: str) -> int: ...
    async def get_latest_allocation(self, provider_id: str, ticker_id: str) -> Optional[dict]: ...
    async def count_active_tickers(self, provider_id: str) -> int: ...
    async def get_strategy(self, strategy_id: str) -> Optional[dict]: ...
    async def count_positions_for_strategy(self, strategy_id: str) -> int: ...


def _is_within_schedule(schedule: dict, now: datetime) -> bool:
    """Evaluate a provider schedule in its own timezone (mirrors isWithinSchedule; fails open
    to UTC on an unknown timezone rather than blocking all trading on a config typo)."""
    tzname = schedule.get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(tzname))
    except Exception:  # noqa: BLE001 — unknown tz -> UTC fallback
        local = now.astimezone(timezone.utc)
    day = DAY_ABBREVIATIONS[int(local.strftime("%w"))]  # %w: Sunday=0..Saturday=6
    hhmm = local.strftime("%H:%M")
    return (
        day in schedule["activeDays"]
        and hhmm >= schedule["activeStartTime"]
        and hhmm <= schedule["activeEndTime"]
    )


async def _resolve_allocation_ceiling(store: RiskGateStore, provider_id: str, ticker_id: str, cum_base: float) -> float:
    snapshot = await store.get_latest_allocation(provider_id, ticker_id)
    if snapshot:
        return _num(snapshot["allocatedCapital"])
    active_ticker_count = await store.count_active_tickers(provider_id)
    return cum_base / active_ticker_count if active_ticker_count > 0 else cum_base


async def _is_strategy_still_in_probation(store: RiskGateStore, strategy_id: Optional[str], probation_trades_count: int) -> bool:
    if not strategy_id:
        return True  # manual/UI order -> probation-sized (conservative)
    strategy = await store.get_strategy(strategy_id)
    if not strategy or strategy.get("status") != "live":
        return True
    trades_since_promotion = await store.count_positions_for_strategy(strategy_id)
    return trades_since_promotion < probation_trades_count


async def evaluate_order_risk_gate(store: RiskGateStore, intent: OrderIntent, now: Optional[datetime] = None) -> dict:
    """Returns {approved, allowedQuantity, isProbation, reason?}. Raises RiskGateHalt when
    trading is halted (daily-loss breach) and RiskGateError on invalid input."""
    now = now or datetime.now(timezone.utc)

    ticker = await store.get_ticker(intent.ticker_id)
    if not ticker:
        raise RiskGateError("Invalid ticker")
    provider_id = ticker["providerId"]
    provider = await store.get_provider(provider_id)
    if not provider:
        raise RiskGateError("Invalid provider for ticker")

    # 0a. Kill switch (spec 5.4) — hard stop, checked first.
    if provider.get("killSwitchActive"):
        return {"approved": False, "allowedQuantity": 0, "isProbation": False,
                "reason": f"Kill switch active for this provider: {provider.get('killSwitchReason') or 'no reason recorded'}."}

    # 0b. Provider trading on/off.
    if not provider.get("tradingEnabled"):
        return {"approved": False, "allowedQuantity": 0, "isProbation": False,
                "reason": "Trading is disabled for this provider."}

    # 0c. Provider schedule (Binance 24/7 -> effectively a no-op).
    schedule = await store.get_schedule(provider_id)
    if schedule and not _is_within_schedule(schedule, now):
        return {"approved": False, "allowedQuantity": 0, "isProbation": False,
                "reason": (f"Outside provider's active trading schedule ({','.join(schedule['activeDays'])} "
                           f"{schedule['activeStartTime']}-{schedule['activeEndTime']} {schedule['timezone']}).")}

    # 1. Risk-limit policy (defaults mirror the backend's in-memory create()).
    risk_limit = await store.get_risk_limit(provider_id) or {
        "dailyLossLimitPct": 2.0, "maxConcurrentPositionsPerTicker": 1,
        "probationSizePct": 25.0, "probationTradesCount": 10,
    }
    daily_loss_limit_pct = _num(risk_limit["dailyLossLimitPct"])

    # 2. Daily loss limit (Section 5.1).
    today = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    daily_tracker = await store.get_today_tracker(provider_id, today)
    latest_balance = await store.get_latest_balance(provider_id)
    cum_base = _num(latest_balance["tradableBalance"]) if latest_balance else 10000.0

    if not daily_tracker:
        await store.create_today_tracker(provider_id, today, cum_base)
        daily_tracker = {"realizedPl": 0, "unrealizedPl": 0, "limitBreached": False}

    if daily_tracker.get("limitBreached"):
        raise RiskGateHalt(
            f"Risk Gate Rejection: Daily loss limit ({daily_loss_limit_pct:.1f}%) breached for provider. "
            "All trading halted until reset."
        )

    net_pl = _num(daily_tracker["realizedPl"]) + _num(daily_tracker["unrealizedPl"])
    current_total_loss_pct = abs(net_pl / cum_base) * 100
    if current_total_loss_pct >= daily_loss_limit_pct and net_pl < 0:
        await store.mark_breached(provider_id, today)
        await store.create_alert(
            "critical", "RISK_GATE_KILL_SWITCH",
            (f"CRITICAL: Daily loss limit breached for provider {provider_id}. "
             f"Realized/Unrealized loss: {current_total_loss_pct:.2f}%. Trading halted, flattening all open positions."),
            "Provider", provider_id,
        )
        try:
            await store.flatten_all_positions(provider_id, f"Daily loss limit breach ({current_total_loss_pct:.2f}%)")
        except Exception:  # noqa: BLE001 — best-effort; must not suppress the halt
            pass
        raise RiskGateHalt(f"Risk Gate Triggered: Daily loss limit breached ({current_total_loss_pct:.2f}%).")

    # 3. Max concurrent positions (Section 5.2).
    max_concurrent = _num(risk_limit["maxConcurrentPositionsPerTicker"])
    active_positions_count = await store.count_open_positions(intent.ticker_id)
    if active_positions_count >= max_concurrent:
        return {"approved": False, "allowedQuantity": 0, "isProbation": False,
                "reason": f"Max concurrent positions limit reached ({risk_limit['maxConcurrentPositionsPerTicker']} max per ticker)."}

    # 4. Position sizing (Section 5.3 & 6) — allocation ceiling × probation multiplier.
    allocated_capital = await _resolve_allocation_ceiling(store, provider_id, intent.ticker_id, cum_base)
    is_probation = await _is_strategy_still_in_probation(store, intent.strategy_id, int(_num(risk_limit["probationTradesCount"])))
    sizing_multiplier = _num(risk_limit["probationSizePct"]) / 100 if is_probation else 1.0
    capital_to_use = allocated_capital * sizing_multiplier
    allowed_quantity = to_fixed(capital_to_use / intent.price, 4)

    return {"approved": True, "allowedQuantity": allowed_quantity, "isProbation": is_probation}
