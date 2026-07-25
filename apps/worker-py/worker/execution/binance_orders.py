"""Binance market-order placement — faithful port of BinanceAdapter.placeMarketOrder.

The binance-connector import is lazy so the execution engine and its tests load without the
SDK, and so nothing can accidentally reach the exchange unless a live order is actually placed.
"""
from __future__ import annotations

from typing import Protocol

MAINNET_REST = "https://api.binance.com"
TESTNET_REST = "https://testnet.binance.vision"


class OrderPlacer(Protocol):
    async def place_market_order(
        self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float
    ) -> dict: ...


class BinanceOrderPlacer:
    """Places a real MARKET order via binance-connector. `side` is 'BUY'/'SELL'."""

    async def place_market_order(
        self, creds: dict, use_testnet: bool, symbol: str, side: str, quantity: float
    ) -> dict:
        from binance.spot import Spot

        base_url = TESTNET_REST if use_testnet else MAINNET_REST
        client = Spot(api_key=creds["apiKey"], api_secret=creds["apiSecret"], base_url=base_url)
        # binance-connector is sync; match the TS newOrder(..., quantity toFixed(8), FULL).
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
