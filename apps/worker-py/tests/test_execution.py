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
    def __init__(self, provider, creds=None, caps=None):
        self.provider = provider
        self.creds = creds
        self.caps = caps
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.api: list[str] = []
        self._n = 0

    def seed(self, side, entry, qty, pid=None):
        self._n += 1
        pid = pid or f"pos{self._n}"
        self.positions[pid] = {"entry_price": entry, "quantity": qty, "side": side, "status": "open"}
        return pid

    async def get_ticker(self, i): return {"id": "t1", "symbol": "BTCUSDT", "providerId": "p1"}
    async def get_provider(self, i): return self.provider
    async def get_credential(self, i, use_testnet=False): return self.creds

    async def insert_position(self, *, ticker_id, strategy_id, side, entry_price, quantity, is_probation):
        return self.seed(side, entry_price, quantity)

    async def insert_order(self, **k): self.orders.append(k)

    async def get_position(self, pid):
        p = self.positions.get(pid)
        if not p:
            return None
        return {"id": pid, "tickerId": "t1", "side": p["side"], "status": p["status"],
                "entryPrice": p["entry_price"], "currentPrice": p["entry_price"], "quantity": p["quantity"],
                "providerId": "p1", "symbol": "BTCUSDT"}

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
    def __init__(self, raises=False, fill=None):
        self.calls = []
        self.raises = raises
        self.fill = fill

    async def place_market_order(self, creds, use_testnet, symbol, side, quantity):
        self.calls.append((symbol, side, quantity))
        if self.raises:
            raise RuntimeError("exchange down")
        return self.fill or {"orderId": "LIVE123", "fillPrice": 101.0, "fillQuantity": quantity, "status": "FILLED"}


PAPER = {"id": "p1", "name": "P", "tradingMode": "paper", "type": "binance", "useTestnet": True}
LIVE = {"id": "p1", "name": "P", "tradingMode": "live", "type": "binance", "useTestnet": True}


async def test_execute_paper_uses_simulated_fill():
    store = _ExecStore(PAPER)
    placer = _Placer()
    r = await ExecutionService(store, _RiskStore(), placer).execute_trade_signal("t1", "long", 100)
    assert r["status"] == "EXECUTED" and r["isLiveOrder"] is False
    assert placer.calls == []  # no exchange call in paper mode
    assert len(store.positions) == 1 and store.orders[0]["side"] == "buy"
    # probation 25% of 10000 even-share / price 100 = 25.
    assert store.orders[0]["quantity"] == 25.0 and store.orders[0]["price"] == 100


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
    assert placer.calls == [("BTCUSDT", "BUY", 25.0)]
    assert store.orders[0]["price"] == 101.0  # exchange fill price, not the signal price
    assert store.api == ["success"]


async def test_execute_live_order_failure_rejects_and_records_failure():
    store = _ExecStore(LIVE, creds={"apiKey": "k", "apiSecret": "s"})
    r = await ExecutionService(store, _RiskStore(), _Placer(raises=True)).execute_trade_signal("t1", "long", 100)
    assert r["status"] == "REJECTED" and "Exchange order placement failed" in r["reason"]
    assert store.positions == {} and store.api == ["failure"]


async def test_execute_live_without_credentials_raises():
    store = _ExecStore(LIVE, creds=None)
    with pytest.raises(Exception):
        await ExecutionService(store, _RiskStore(), _Placer()).execute_trade_signal("t1", "long", 100)


async def test_close_long_pnl():
    store = _ExecStore(PAPER)
    pid = store.seed("long", entry=100, qty=2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 110)
    assert r["realizedPl"] == 20.0  # (110-100)*2
    assert store.positions[pid]["status"] == "closed" and store.orders[-1]["side"] == "sell"


async def test_close_short_pnl():
    store = _ExecStore(PAPER)
    pid = store.seed("short", entry=100, qty=2)
    r = await ExecutionService(store, _RiskStore(), _Placer()).close_position(pid, 90)
    assert r["realizedPl"] == 20.0  # (100-90)*2
    assert store.orders[-1]["side"] == "buy"


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
