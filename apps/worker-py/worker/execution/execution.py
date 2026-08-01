"""Order execution engine — faithful port of execution.service.ts (Phase 3c-2).

executeTradeSignal / closePosition / flattenAllPositionsForProvider / enforceHardExits,
including the exact gate → (live order OR simulated fill) → position + order write flow and
the P/L math. A real exchange order is placed ONLY when the provider is explicitly in 'live'
mode, is a Binance provider, and has stored credentials — otherwise a simulated local fill is
recorded (SIM_* order id) seeded by the real streamed price.

This engine is NOT wired to the live tick loop here; that integration + cutover is the final
step (3c-3). DB work is behind ExecutionStore; order placement behind OrderPlacer — so the
whole flow is unit-tested on the simulated path with fakes, never touching Binance.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional, Protocol

from ..util import to_fixed
from .binance_orders import OrderPlacer
from .risk_gate import OrderIntent, RiskGateStore, evaluate_order_risk_gate

log = logging.getLogger("worker.execution")

LONG = "long"
SHORT = "short"
LIVE = "live"
BINANCE = "binance"
FUTURES = "futures"


class ExecutionError(Exception):
    """Invalid input (mirrors BadRequestException)."""


class ExecutionStore(Protocol):
    async def get_ticker(self, ticker_id: str) -> Optional[dict]: ...
    async def get_provider(self, provider_id: str) -> Optional[dict]: ...
    async def get_credential(self, provider_id: str, use_testnet: bool = False) -> Optional[dict]: ...
    async def insert_position(self, *, ticker_id: str, strategy_id: Optional[str], side: str,
                              entry_price: float, quantity: float, is_probation: bool,
                              leverage: int = 1, margin_type: str = "isolated",
                              trade_mode: str = "paper", network: Optional[str] = None) -> str: ...
    async def insert_order(self, *, position_id: str, provider_order_id: str, side: str,
                           quantity: float, price: float) -> None: ...
    async def get_position(self, position_id: str) -> Optional[dict]: ...
    async def close_position_row(self, *, position_id: str, exit_price: float, realized_pl: float) -> None: ...
    async def find_open_positions_by_ticker(self, ticker_id: str) -> list[dict]: ...
    async def find_open_positions_by_provider(self, provider_id: str) -> list[dict]: ...
    async def get_hard_caps(self, provider_id: str) -> Optional[dict]: ...
    async def record_api_success(self, provider_id: str) -> None: ...
    async def record_api_failure(self, provider_id: str) -> None: ...
    async def update_position_mark(self, position_id: str, current_price: float, unrealized_pl: float) -> None: ...


def _num(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class ExecutionService:
    def __init__(self, store: ExecutionStore, risk_store: RiskGateStore, order_placer: OrderPlacer,
                 futures_order_placer: Optional[OrderPlacer] = None) -> None:
        self.store = store
        self.risk_store = risk_store
        self.orders = order_placer
        # Futures venue (Phase 4b). Falls back to the spot placer if not supplied, so nothing breaks
        # in spot-only setups / tests; a futures ticker without a futures placer would just error at
        # order time rather than silently place a wrong-venue order.
        self.futures_orders = futures_order_placer or order_placer

    def _placer_for(self, market_type: Optional[str]) -> OrderPlacer:
        return self.futures_orders if (market_type or "").lower() == FUTURES else self.orders

    async def execute_trade_signal(self, ticker_id: str, side: str, price: float,
                                   strategy_id: Optional[str] = None,
                                   requested_quantity: Optional[float] = None) -> dict:
        # 1. Unbypassable risk gate (may raise RiskGateHalt on daily-loss breach). A
        # requested_quantity replaces only the gate's SIZING step — every policy check still runs.
        risk = await evaluate_order_risk_gate(
            self.risk_store,
            OrderIntent(ticker_id, side, price, strategy_id, requested_quantity=requested_quantity),
        )
        if not risk["approved"]:
            return {"status": "REJECTED", "reason": risk.get("reason")}

        ticker = await self.store.get_ticker(ticker_id)
        if not ticker:
            raise ExecutionError("Invalid ticker")
        provider = await self.store.get_provider(ticker["providerId"])

        # Phase 4b: futures tickers route to the futures venue and carry leverage. 1x default keeps a
        # futures long economically identical to spot until leverage is deliberately turned up later.
        is_futures = (ticker.get("marketType") or "").lower() == FUTURES
        leverage = 1

        # A SHORT is a SELL-to-open, which only the futures venue accepts. Refuse it on spot
        # instead of simulating a fill: a paper record of a trade the exchange would have rejected
        # is worse than no trade at all, because it silently flatters measured performance and the
        # gap only surfaces on the first live order. Tickers are created on futures now, so this
        # should never fire — it exists to make a misconfiguration loud rather than profitable.
        if side == SHORT and not is_futures:
            return {
                "status": "REJECTED",
                "reason": (
                    f"Cannot open a short on a '{ticker.get('marketType') or 'spot'}' ticker — "
                    "selling to open requires the futures venue."
                ),
            }

        fill_price = price
        fill_quantity = risk["allowedQuantity"]
        provider_order_id = f"SIM_{int(time.time() * 1000)}"
        is_live_order = False

        # 2. Real order ONLY in live + binance + creds; else simulated fill.
        if provider and provider.get("tradingMode") == LIVE and provider.get("type") == BINANCE:
            creds = await self.store.get_credential(provider["id"], provider.get("useTestnet", False))
            if not creds or not creds.get("apiKey") or not creds.get("apiSecret"):
                raise ExecutionError(
                    f"Provider {provider.get('name')} is set to 'live' trading mode but has no stored API credentials for the "
                    f"{'testnet' if provider.get('useTestnet') else 'mainnet'} network — cannot place a real order."
                )
            try:
                # A SHORT open is a SELL-to-open — only valid on the futures venue; the placer is
                # chosen by the ticker's market type so a spot ticker never routes a short there.
                order_side = "BUY" if side == LONG else "SELL"
                placer = self._placer_for(ticker.get("marketType"))
                result = await placer.place_market_order(
                    creds, provider.get("useTestnet", False), ticker["symbol"], order_side,
                    risk["allowedQuantity"], reduce_only=False, leverage=leverage,
                )
                fill_price = result.get("fillPrice") or price
                fill_quantity = result.get("fillQuantity") or risk["allowedQuantity"]
                provider_order_id = result["orderId"]
                is_live_order = True
                await self.store.record_api_success(provider["id"])
            except Exception as err:  # noqa: BLE001
                log.error("Live order placement failed on Binance for %s: %s", ticker["symbol"], err)
                await self.store.record_api_failure(provider["id"])
                return {"status": "REJECTED", "reason": f"Exchange order placement failed: {err}"}

        # Stamp HOW the fill happened so the dashboard can filter by mode+network (a paper/simulated
        # fill is network-agnostic → network stays NULL).
        use_testnet = bool(provider.get("useTestnet")) if provider else False
        position_id = await self.store.insert_position(
            ticker_id=ticker_id, strategy_id=strategy_id, side=side,
            entry_price=fill_price, quantity=fill_quantity, is_probation=risk["isProbation"],
            leverage=leverage if is_futures else 1, margin_type="isolated" if is_futures else "cross",
            trade_mode=LIVE if is_live_order else "paper",
            network=("testnet" if use_testnet else "mainnet") if is_live_order else None,
        )
        await self.store.insert_order(
            position_id=position_id, provider_order_id=provider_order_id,
            side="buy" if side == LONG else "sell", quantity=fill_quantity, price=fill_price,
        )
        return {"status": "EXECUTED", "positionId": position_id, "isLiveOrder": is_live_order}

    async def close_position(self, position_id: str, exit_price: float) -> dict:
        position = await self.store.get_position(position_id)
        if not position or position["status"] == "closed":
            raise ExecutionError("Position not found or already closed")

        final_exit_price = exit_price
        provider_order_id = f"SIM_exit_{int(time.time() * 1000)}"

        provider = await self.store.get_provider(position["providerId"]) if position.get("providerId") else None
        if provider and provider.get("tradingMode") == LIVE and provider.get("type") == BINANCE:
            creds = await self.store.get_credential(provider["id"], provider.get("useTestnet", False))
            if creds and creds.get("apiKey") and creds.get("apiSecret"):
                try:
                    # Cover a short with a BUY, close a long with a SELL. reduce_only guarantees the
                    # order can only flatten the position, never accidentally open the opposite side.
                    close_side = "SELL" if position["side"] == LONG else "BUY"
                    placer = self._placer_for(position.get("marketType"))
                    result = await placer.place_market_order(
                        creds, provider.get("useTestnet", False), position["symbol"], close_side,
                        _num(position["quantity"]), reduce_only=True,
                    )
                    final_exit_price = result.get("fillPrice") or exit_price
                    provider_order_id = result["orderId"]
                except Exception as err:  # noqa: BLE001
                    log.error("Live close-order failed for position %s: %s", position_id, err)
                    raise ExecutionError(f"Exchange close-order failed: {err}")

        entry = _num(position["entryPrice"])
        qty = _num(position["quantity"])
        price_diff = final_exit_price - entry
        realized_pl = to_fixed((price_diff if position["side"] == LONG else -price_diff) * qty, 8)

        await self.store.close_position_row(position_id=position_id, exit_price=final_exit_price, realized_pl=realized_pl)
        await self.store.insert_order(
            position_id=position_id, provider_order_id=provider_order_id,
            side="sell" if position["side"] == LONG else "buy", quantity=qty, price=final_exit_price,
        )
        return {"positionId": position_id, "realizedPl": realized_pl}

    async def flatten_all_positions_for_provider(self, provider_id: str, reason: str) -> dict:
        positions = await self.store.find_open_positions_by_provider(provider_id)
        closed: list[str] = []
        for position in positions:
            try:
                exit_price = _num(position.get("currentPrice")) or _num(position["entryPrice"])
                result = await self.close_position(position["id"], exit_price)
                closed.append(result["positionId"])
            except Exception as err:  # noqa: BLE001
                log.error("Auto-flatten failed for position %s (%s): %s", position["id"], reason, err)
        return {"flattenedCount": len(closed), "positionIds": closed}

    async def enforce_hard_exits(self, ticker_id: str, last_price: float) -> None:
        positions = await self.store.find_open_positions_by_ticker(ticker_id)
        if not positions:
            return

        caps_by_provider: dict[str, Optional[dict]] = {}
        for pos in positions:
            provider_id = pos.get("providerId")
            if not provider_id:
                continue
            if provider_id not in caps_by_provider:
                caps_by_provider[provider_id] = await self.store.get_hard_caps(provider_id)
            limit = caps_by_provider[provider_id]
            if not limit:
                continue

            sl = _num(limit["hardStopLossPct"]) if limit.get("hardStopLossPct") is not None else None
            tp = _num(limit["hardTakeProfitPct"]) if limit.get("hardTakeProfitPct") is not None else None
            if sl is None and tp is None:
                continue

            entry = _num(pos["entryPrice"])
            if not entry:
                continue
            pnl_pct = ((last_price - entry) / entry) * 100 if pos["side"] == LONG else ((entry - last_price) / entry) * 100

            if sl is not None and pnl_pct <= -sl:
                log.warning("Hard stop-loss hit for position %s (%.2f%% <= -%s%%). Closing.", pos["id"], pnl_pct, sl)
                try:
                    await self.close_position(pos["id"], last_price)
                except Exception as e:  # noqa: BLE001
                    log.error("Hard stop-loss close failed for %s: %s", pos["id"], e)
            elif tp is not None and pnl_pct >= tp:
                log.warning("Hard take-profit hit for position %s (%.2f%% >= %s%%). Closing.", pos["id"], pnl_pct, tp)
                try:
                    await self.close_position(pos["id"], last_price)
                except Exception as e:  # noqa: BLE001
                    log.error("Hard take-profit close failed for %s: %s", pos["id"], e)
