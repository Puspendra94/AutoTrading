"""Execution engine tests (Phase 3c-2) — simulated path + live-branch logic with fakes.

No real Binance calls: the OrderPlacer is faked. Covers the gate→fill→persist flow, the
live-vs-simulated branch, P/L math, hard stop-loss/take-profit exits, and flatten.
"""
import pytest

from worker.execution.execution import ExecutionService


class _RiskStore:
    """Approves by default; set open_positions to force a max-concurrent rejection."""
    def __init__(self, open_positions=0):
        self.open_positions = open_positions

    async def get_ticker(self, i): return {"id": "t1", "providerId": "p1"}
    async def get_provider(self, i): return {"id": "p1", "tradingEnabled": True, "killSwitchActive": False, "killSwitchReason": None}
    async def get_schedule(self, i): return None
    async def get_risk_limit(self, i): return {"dailyLossLimitPct": 2.0, "maxConcurrentPositionsPerTicker": 1, "probationSizePct": 25.0, "probationTradesCount": 10}
    async def get_today_tracker(self, i, t): return {"limitBreached": False, "realizedPl": 0, "unrealizedPl": 0}
    async def get_latest_balance(self, i): return {"tradableBalance": 10000}
    async def create_today_tracker(self, *a): pass
    async def mark_breached(self, *a): pass
    async def create_alert(self, *a): pass
    async def flatten_all_positions(self, *a): pass
    async def count_open_positions(self, i): return self.open_positions
    async def get_latest_allocation(self, p, t): return None
    async def count_active_tickers(self, i): return 1
    async def get_strategy(self, i): return None
    async def count_positions_for_strategy(self, i): return 0


class _ExecStore:
    def __init__(self, provider, creds=None, caps=None, market_type=None):
        self.provider = provider
        self.creds = creds
        self.caps = caps
        self.market_type = market_type
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.api: list[str] = []
        self._n = 0

    def seed(self, side, entry, qty, pid=None):
        self._n += 1
        pid = pid or f"pos{self._n}"
        self.positions[pid] = {"entry_price": entry, "quantity": qty, "side": side, "status": "open"}
        return pid

    async def get_ticker(self, i): return {"id": "t1", "symbol": "BTCUSDT", "providerId": "p1", "marketType": self.market_type}
    async def get_provider(self, i): return self.provider
    async def get_credential(self, i, use_testnet=False): return self.creds

    async def insert_position(self, *, ticker_id, strategy_id, side, entry_price, quantity, is_probation,
                              leverage=1, margin_type="isolated", trade_mode="paper", network=None):
        pid = self.seed(side, entry_price, quantity)
        self.positions[pid].update(leverage=leverage, margin_type=margin_type, trade_mode=trade_mode, network=network)
        return pid

    async def insert_order(self, **k): self.orders.append(k)

    async def get_position(self, pid):
        p = self.positions.get(pid)
        if not p:
            return None
        return {"id": pid, "tickerId": "t1", "side": p["side"], "status": p["status"],
                "entryPrice": p["entry_price"], "currentPrice": p["entry_price"], "quantity": p["quantity"],
                "providerId": "p1", "symbol": "BTCUSDT", "marketType": self.market_type}

    async def close_position_row(self, *, position_id, exit_price, realized_pl):
        self.positions[position_id]["status"] = "closed"
        self.positions[position_id]["realized_pl"] = realized_pl

    async def find_open_positions_by_ticker(self, i):
        return [{"id": pid, "side": p["side"], "entryPrice": p["entry_price"], "currentPrice": p["entry_price"],
                 "quantity": p["quantity"], "providerId": "p1"} for pid, p in self.positions.items() if p["status"] == "open"]

    async def find_open_positions_by_provider(self, i):
        return [{"id": pid, "entryPrice": p["entry_price"], "currentPrice": p["entry_price"]}
                for pid, p in self.positions.items() if p["status"] == "open"]

    async def get_hard_caps(self, i): return self.caps
    async def record_api_success(self, i): self.api.append("success")
    async def record_api_failure(self, i): self.api.append("failure")


class _Placer:
    def __init__(self, raises=False, fill=None, stop_raises=False):
        self.calls = []
        self.stops = []
        self.cancels = []
        self.raises = raises
        self.stop_raises = stop_raises
        self.fill = fill

    async def place_market_order(self, creds, use_testnet, symbol, side, quantity, *, reduce_only=False, leverage=1):
        self.calls.append((symbol, side, quantity, reduce_only, leverage))
        if self.raises:
            raise RuntimeError("exchange down")
        return self.fill or {"orderId": "LIVE123", "fillPrice": 101.0, "fillQuantity": quantity, "status": "FILLED"}

    async def place_stop_order(self, creds, use_testnet, symbol, side, stop_price):
        if self.stop_raises:
            raise RuntimeError("stop rejected")
        self.stops.append((symbol, side, stop_price))
        return {"orderId": f"STOP{len(self.stops)}", "stopPrice": stop_price}

    async def cancel_open_orders(self, creds, use_testnet, symbol):
        self.cancels.append(symbol)


PAPER = {"id": "p1", "name": "P", "tradingMode": "paper", "type": "binance", "useTestnet": True}
LIVE = {"id": "p1", "name": "P", "tradingMode": "live", "type": "binance", "useTestnet": True}

# Costs the engine now charges (worker.config defaults): 0.10% taker/side on spot, 0.04% on
# futures, plus 0.02% of simulated slippage per leg. Spelled out here so a config change that
# silently zeroes them fails a test rather than quietly flattering every paper result again.
SPOT_FEE, FUTURES_FEE, SLIP = 0.001, 0.0004, 0.0002


async def test_execute_paper_uses_simulated_fill():
    store = _ExecStore(PAPER)
    placer = _Placer()
    r = await ExecutionService(store, _RiskStore(), placer).execute_trade_signal("t1", "long", 100)
    assert r["status"] == "EXECUTED" and r["isLiveOrder"] is False
    assert placer.calls == []  # no exchange call in paper mode
    assert len(store.positions) == 1 and store.orders[0]["side"] == "buy"
    # probation 25% of 10000 even-share / price 100 = 25.
    assert store.orders[0]["quantity"] == 25.0
    # A simulated BUY crosses the spread like a real one, so the fill is ABOVE the signal price.
    assert store.orders[0]["price"] == pytest.approx(100 * (1 + SLIP))


async def test_execute_rejected_by_gate_persists_nothing():
    store = _ExecStore(PAPER)
    r = await ExecutionService(store, _RiskStore(open_positions=1), _Placer()).execute_trade_signal("t1", "long", 100)
    assert r["status"] == "REJECTED"
    assert store.positions == {} and store.orders == []


async def test_execute_live_places_real_order_and_uses_fill():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"})
    placer = _Placer()
    r = await ExecutionService(store, _RiskStore(), placer).execute_trade_signal("t1", "long", 100)
    assert r["isLiveOrder"] is True
    assert placer.calls == [("BTCUSDT", "BUY", 25.0, False, 1)]  # spot open: BUY, not reduceOnly, 1x
    assert store.orders[0]["price"] == 101.0  # exchange fill price, not the signal price
    assert store.api == ["success"]


async def test_execute_live_short_routes_to_futures_venue():
    """Phase 4b: a SHORT open on a futures ticker places a SELL-to-open on the FUTURES placer
    (not the spot one), non-reduceOnly, and records the position as a short with leverage stamped."""
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    spot, futures = _Placer(), _Placer()
    svc = ExecutionService(store, _RiskStore(), spot, futures)
    r = await svc.execute_trade_signal("t1", "short", 100)
    assert r["isLiveOrder"] is True
    assert spot.calls == []                                    # spot venue untouched
    assert futures.calls == [("BTCUSDT", "SELL", 25.0, False, 1)]  # SELL-to-open short, 1x
    pos = list(store.positions.values())[0]
    assert pos["side"] == "short" and pos["leverage"] == 1 and pos["trade_mode"] == "live" and pos["network"] == "testnet"


async def test_close_short_on_futures_covers_reduce_only():
    """Covering a short closes with a reduceOnly BUY on the futures venue."""
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    spot, futures = _Placer(), _Placer()
    pid = store.seed("short", entry=100, qty=2)
    await ExecutionService(store, _RiskStore(), spot, futures).close_position(pid, 90)
    assert spot.calls == []
    assert futures.calls == [("BTCUSDT", "BUY", 2.0, True, 1)]  # BUY-to-cover, reduceOnly


async def test_execute_live_order_failure_rejects_and_records_failure():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"})
    r = await ExecutionService(store, _RiskStore(), _Placer(raises=True)).execute_trade_signal("t1", "long", 100)
    assert r["status"] == "REJECTED" and "Exchange order placement failed" in r["reason"]
    assert store.positions == {} and store.api == ["failure"]


async def test_execute_live_without_credentials_raises():
    store = _ExecStore(LIVE, creds=None)
    with pytest.raises(Exception):
        await ExecutionService(store, _RiskStore(), _Placer()).execute_trade_signal("t1", "long", 100)


async def test_close_long_pnl_is_net_of_costs():
    store = _ExecStore(PAPER)
    pid = store.seed("long", entry=100, qty=2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 110)
    exit_fill = 110 * (1 - SLIP)                       # selling to close crosses the spread down
    expected = (exit_fill - 100) * 2 - (100 * 2 + exit_fill * 2) * SPOT_FEE
    assert r["realizedPl"] == pytest.approx(expected)
    assert r["realizedPl"] < 20.0                      # the gross figure this used to book
    assert store.positions[pid]["status"] == "closed" and store.orders[-1]["side"] == "sell"


async def test_close_short_pnl_is_net_of_costs():
    store = _ExecStore(PAPER)
    pid = store.seed("short", entry=100, qty=2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 90)
    exit_fill = 90 * (1 + SLIP)                        # buying to cover crosses the spread up
    expected = (100 - exit_fill) * 2 - (100 * 2 + exit_fill * 2) * SPOT_FEE
    assert r["realizedPl"] == pytest.approx(expected)
    assert r["realizedPl"] < 20.0
    assert store.orders[-1]["side"] == "buy"


async def test_futures_pays_the_futures_fee_not_the_spot_one():
    store = _ExecStore(PAPER, market_type="futures")
    pid = store.seed("long", entry=100, qty=2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 110)
    exit_fill = 110 * (1 - SLIP)
    expected = (exit_fill - 100) * 2 - (100 * 2 + exit_fill * 2) * FUTURES_FEE
    assert r["realizedPl"] == pytest.approx(expected)
    assert r["fees"] == pytest.approx((100 * 2 + exit_fill * 2) * FUTURES_FEE)


async def test_a_scratch_trade_still_costs_money():
    """The fact a fee-free paper record hid: closing exactly where you opened is a LOSS."""
    store = _ExecStore(PAPER, market_type="futures")
    pid = store.seed("long", entry=100, qty=10)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 100)
    assert r["realizedPl"] < 0
    assert r["fees"] > 0


async def test_enforce_hard_stop_loss_closes():
    store = _ExecStore(PAPER, caps={"hardStopLossPct": 5, "hardTakeProfitPct": None})
    pid = store.seed("long", entry=100, qty=1)
    await ExecutionService(store, _RiskStore(), _Placer()).enforce_hard_exits("t1", 94)  # -6% <= -5%
    assert store.positions[pid]["status"] == "closed"


async def test_enforce_hard_exits_within_limits_noop():
    store = _ExecStore(PAPER, caps={"hardStopLossPct": 5, "hardTakeProfitPct": 10})
    pid = store.seed("long", entry=100, qty=1)
    await ExecutionService(store, _RiskStore(), _Placer()).enforce_hard_exits("t1", 98)  # -2%
    assert store.positions[pid]["status"] == "open"


async def test_enforce_hard_exits_null_caps_noop():
    store = _ExecStore(PAPER, caps={"hardStopLossPct": None, "hardTakeProfitPct": None})
    pid = store.seed("long", entry=100, qty=1)
    await ExecutionService(store, _RiskStore(), _Placer()).enforce_hard_exits("t1", 50)
    assert store.positions[pid]["status"] == "open"


async def test_flatten_closes_all_open_for_provider():
    store = _ExecStore(PAPER)
    store.seed("long", 100, 1)
    store.seed("long", 200, 2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).flatten_all_positions_for_provider("p1", "breach")
    assert r["flattenedCount"] == 2
    assert all(p["status"] == "closed" for p in store.positions.values())


# --- Protective stop resting AT THE EXCHANGE --------------------------------------------------

async def test_protective_stop_is_a_noop_in_paper():
    """Paper mode never reaches the exchange, so the software ladder is the whole protection."""
    store = _ExecStore(PAPER, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    placer = _Placer()
    order_id = await ExecutionService(store, _RiskStore(), placer, placer).sync_protective_stop(pid, 95)
    assert order_id is None
    assert placer.stops == [] and placer.cancels == []


async def test_protective_stop_rests_on_the_exchange_when_live():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    placer = _Placer()
    order_id = await ExecutionService(store, _RiskStore(), placer, placer).sync_protective_stop(pid, 95)
    assert order_id == "STOP1"
    # SELL protects a long, and anything previously resting is cleared first so a ratchet cannot
    # leave two stops on the same position.
    assert placer.stops == [("BTCUSDT", "SELL", 95)]
    assert placer.cancels == ["BTCUSDT"]


async def test_protective_stop_covers_a_short_with_a_buy():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("short", entry=100, qty=1)
    placer = _Placer()
    await ExecutionService(store, _RiskStore(), placer, placer).sync_protective_stop(pid, 105)
    assert placer.stops == [("BTCUSDT", "BUY", 105)]


async def test_ratchet_replaces_rather_than_stacks():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    placer = _Placer()
    svc = ExecutionService(store, _RiskStore(), placer, placer)
    await svc.sync_protective_stop(pid, 95)
    await svc.sync_protective_stop(pid, 98)
    assert [s[2] for s in placer.stops] == [95, 98]
    assert placer.cancels == ["BTCUSDT", "BTCUSDT"]


async def test_a_failed_stop_order_does_not_break_the_trade():
    """The software ladder still holds the stop, so this is loud but survivable."""
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    placer = _Placer(stop_raises=True)
    order_id = await ExecutionService(store, _RiskStore(), placer, placer).sync_protective_stop(pid, 95)
    assert order_id is None


async def test_no_stop_is_placed_for_a_closed_position():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    store.positions[pid]["status"] = "closed"
    placer = _Placer()
    assert await ExecutionService(store, _RiskStore(), placer, placer).sync_protective_stop(pid, 95) is None
    assert placer.stops == []


async def test_live_close_clears_resting_orders():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"}, market_type="futures")
    pid = store.seed("long", entry=100, qty=1)
    placer = _Placer()
    await ExecutionService(store, _RiskStore(), placer, placer).close_position(pid, 110)
    assert placer.cancels == ["BTCUSDT"]
