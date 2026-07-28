"""Binance market-order placement.

Two venues behind one OrderPlacer Protocol:
  * BinanceOrderPlacer  — SPOT (faithful port of BinanceAdapter.placeMarketOrder).
  * FuturesOrderPlacer  — USD-M FUTURES (Phase 4b): enables SHORT execution (SELL-to-open /
    BUY reduceOnly to cover) and leverage. Sets leverage + isolated margin before opening.

Both SDKs (binance-connector / binance-futures-connector) are synchronous and imported lazily,
so the engine and its tests load without either SDK and nothing reaches an exchange unless a live
order is actually placed. The sync HTTP calls run via asyncio.to_thread so placing an order never
blocks the shared event loop that also drives the live kline stream's websocket pongs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger("worker.execution.orders")

SPOT_MAINNET_REST = "https://api.binance.com"
SPOT_TESTNET_REST = "https://testnet.binance.vision"
FUTURES_MAINNET_REST = "https://fapi.binance.com"
FUTURES_TESTNET_REST = "https://testnet.binancefuture.com"

# Binance returns -4046 when the requested margin type is already set — a no-op, not an error.
_MARGIN_TYPE_UNCHANGED = "-4046"


class OrderPlacer(Protocol):
    async def place_market_order(
        self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float,
        *, reduce_only: bool = False, leverage: int = 1,
    ) -> dict: ...


class BinanceOrderPlacer:
    """Places a real SPOT MARKET order via binance-connector. `side` is 'BUY'/'SELL'.
    Spot can't short, so `reduce_only`/`leverage` are accepted for a uniform interface but ignored."""

    async def place_market_order(
        self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float,
        *, reduce_only: bool = False, leverage: int = 1,
    ) -> dict:
        return await asyncio.to_thread(self._place, creds, use_testnet, symbol, side, quantity)

    def _place(self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float) -> dict:
        from binance.spot import Spot

        base_url = SPOT_TESTNET_REST if use_testnet else SPOT_MAINNET_REST
        client = Spot(api_key=creds["apiKey"], api_secret=creds["apiSecret"], base_url=base_url)
        res = client.new_order(
            symbol=symbol, side=side, type="MARKET",
            quantity=f"{quantity:.8f}", newOrderRespType="FULL",
        )
        fills = res.get("fills") or []
        total_qty = sum(float(f["qty"]) for f in fills) or float(res.get("executedQty", "0"))
        if fills:
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
        else:
            avg_price = float(res.get("price", "0"))
        return {
            "orderId": str(res["orderId"]),
            "fillPrice": avg_price,
            "fillQuantity": total_qty,
            "status": res["status"],
            "raw": res,
        }


class FuturesOrderPlacer:
    """Places a real USD-M FUTURES MARKET order via binance-futures-connector.

    `side` is 'BUY'/'SELL'. A SELL open is a real SHORT (the whole point of Phase 4b). Before an
    OPENING order the symbol's leverage + isolated margin are set (idempotent); a CLOSING order
    passes reduceOnly so it can only reduce/flatten the position, never accidentally flip it."""

    async def place_market_order(
        self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float,
        *, reduce_only: bool = False, leverage: int = 1,
    ) -> dict:
        return await asyncio.to_thread(
            self._place, creds, use_testnet, symbol, side, quantity, reduce_only, leverage
        )

    def _place(self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float,
               reduce_only: bool, leverage: int) -> dict:
        from binance.um_futures import UMFutures

        base_url = FUTURES_TESTNET_REST if use_testnet else FUTURES_MAINNET_REST
        client = UMFutures(key=creds["apiKey"], secret=creds["apiSecret"], base_url=base_url)

        # Configure the symbol only when OPENING (a reduceOnly close inherits the live settings).
        if not reduce_only:
            self._configure_symbol(client, symbol, leverage)

        params = {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": f"{quantity:.8f}", "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        res = client.new_order(**params)

        exec_qty = float(res.get("executedQty") or quantity)
        avg = float(res.get("avgPrice") or 0) or 0.0
        return {
            "orderId": str(res["orderId"]),
            "fillPrice": avg,  # 0.0 -> caller falls back to the streamed price
            "fillQuantity": exec_qty,
            "status": res.get("status", "NEW"),
            "raw": res,
        }

    @staticmethod
    def _configure_symbol(client, symbol: str, leverage: int) -> None:
        try:
            client.change_leverage(symbol=symbol, leverage=max(1, int(leverage)))
        except Exception as err:  # noqa: BLE001 — leverage set is best-effort; order still sizes correctly
            log.warning("Could not set leverage %sx on %s: %s", leverage, symbol, err)
        try:
            client.change_margin_type(symbol=symbol, marginType="ISOLATED")
        except Exception as err:  # noqa: BLE001 — -4046 = already isolated; any other error is non-fatal
            if _MARGIN_TYPE_UNCHANGED not in str(err):
                log.warning("Could not set ISOLATED margin on %s: %s", symbol, err)
