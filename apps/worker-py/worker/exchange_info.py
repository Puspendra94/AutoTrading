"""Binance symbol filters (LOT_SIZE / MIN_NOTIONAL), cached.

Nothing in this codebase used to fetch these, and it was a live bug waiting to happen: the risk
gate rounded quantity to 4 decimals and the order placer formatted it as `%.8f`, while BTCUSDT
USD-M futures requires multiples of **0.001** and a minimum notional of **50 USDT**. A computed
0.0148 BTC would have gone out as "0.01480000" and been rejected by the exchange.

Sizing has to know these numbers to produce a quantity that can actually be filled, so this is
not deferrable to the point where real orders are switched on — a paper fill computed against
impossible quantities is a simulation of something that cannot happen.

Filters are public data (no auth) and effectively static, so they are fetched once per symbol
per process and cached. A fetch failure falls back to conservative BTCUSDT-like defaults rather
than raising: sizing that is slightly too coarse is recoverable, sizing that crashes the live
loop is not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import config

log = logging.getLogger("worker.exchange_info")

SPOT_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
FUTURES_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

_FETCH_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    step_size: float      # quantity must be a multiple of this (LOT_SIZE)
    min_qty: float        # smallest tradeable quantity (LOT_SIZE)
    min_notional: float   # smallest tradeable quantity * price (MIN_NOTIONAL)
    tick_size: float      # price increment (PRICE_FILTER)

    def round_quantity(self, quantity: float) -> float:
        """Round DOWN to a valid step. Down, never nearest: rounding up can push the order past
        a risk limit that was just checked against the unrounded number."""
        if self.step_size <= 0:
            return quantity
        steps = int(quantity / self.step_size + 1e-9)  # epsilon absorbs binary float error
        return round(steps * self.step_size, 12)

    def is_tradeable(self, quantity: float, price: float) -> tuple[bool, str]:
        """Whether the exchange would accept this order, with the reason when it would not."""
        if quantity < self.min_qty:
            return False, (f"quantity {quantity:.8f} is below the exchange minimum "
                           f"{self.min_qty:.8f} for {self.symbol}")
        notional = quantity * price
        if notional < self.min_notional:
            return False, (f"notional {notional:.2f} USDT is below the exchange minimum "
                           f"{self.min_notional:.2f} for {self.symbol}")
        return True, ""


# Conservative stand-ins used only when the exchange cannot be reached. Matching BTCUSDT USD-M
# means a fallback errs toward rejecting orders that are too small, never toward placing
# something the exchange would bounce.
FALLBACK = SymbolFilters(symbol="", step_size=0.001, min_qty=0.001, min_notional=50.0, tick_size=0.1)

_cache: dict[tuple[str, bool], SymbolFilters] = {}


def _parse(symbol: str, entry: dict) -> SymbolFilters:
    by_type = {f["filterType"]: f for f in entry.get("filters", [])}
    lot = by_type.get("LOT_SIZE", {})
    # Spot uses MIN_NOTIONAL, futures uses the same name but only carries `notional`.
    notional_filter = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL") or {}
    price_filter = by_type.get("PRICE_FILTER", {})
    return SymbolFilters(
        symbol=symbol,
        step_size=float(lot.get("stepSize") or FALLBACK.step_size),
        min_qty=float(lot.get("minQty") or FALLBACK.min_qty),
        min_notional=float(
            notional_filter.get("notional") or notional_filter.get("minNotional")
            or FALLBACK.min_notional
        ),
        tick_size=float(price_filter.get("tickSize") or FALLBACK.tick_size),
    )


async def get_symbol_filters(symbol: str, *, futures: bool = True) -> SymbolFilters:
    """Filters for a symbol, cached per process. Never raises."""
    if not symbol:
        # This happened for real: the caller read `symbol` from a store that does not return it,
        # so an empty string went to the exchange, the lookup failed, and the fallback — which
        # matches BTCUSDT futures — made it look like it had worked. Say so loudly instead.
        log.error("get_symbol_filters called with an EMPTY symbol — using fallback filters. "
                  "The caller is reading the symbol from the wrong store.")
        return FALLBACK

    key = (symbol.upper(), futures)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    url = FUTURES_INFO_URL if futures else SPOT_INFO_URL
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
            resp = await client.get(url, params={"symbol": symbol.upper()})
            resp.raise_for_status()
            payload = resp.json()
        entry = next(
            (s for s in payload.get("symbols", []) if s["symbol"].upper() == symbol.upper()), None
        )
        if entry is None:
            raise ValueError(f"{symbol} not present in exchangeInfo")
        filters = _parse(symbol.upper(), entry)
    except Exception as err:  # noqa: BLE001 — a filter fetch must never break the trading loop
        log.warning("Could not fetch exchange filters for %s (%s); using conservative defaults.",
                    symbol, err)
        filters = SymbolFilters(
            symbol=symbol.upper(), step_size=FALLBACK.step_size, min_qty=FALLBACK.min_qty,
            min_notional=FALLBACK.min_notional, tick_size=FALLBACK.tick_size,
        )

    _cache[key] = filters
    log.info("Exchange filters for %s: step=%s minQty=%s minNotional=%s",
             filters.symbol, filters.step_size, filters.min_qty, filters.min_notional)
    return filters


def clear_cache() -> None:
    """Test hook."""
    _cache.clear()
