"""Risk-gate parity tests (Phase 3c-1).

The first four mirror risk-gate.service.spec.ts one-for-one (same inputs -> same decision);
the rest pin the sizing math, probation logic, and the daily-loss breach side-effects.
"""
import pytest

from worker.execution.risk_gate import (
    OrderIntent,
    RiskGateHalt,
    evaluate_order_risk_gate,
)


class FakeStore:
    """Mirrors the jest mocks in risk-gate.service.spec.ts; every read is overridable."""

    def __init__(self, **overrides):
        self.d = {
            "ticker": {"id": "t1", "providerId": "p1"},
            "provider": {"id": "p1", "tradingEnabled": True, "killSwitchActive": False, "killSwitchReason": None},
            "schedule": None,
            "risk_limit": {"dailyLossLimitPct": 2.0, "maxConcurrentPositionsPerTicker": 1,
                           "probationSizePct": 25.0, "probationTradesCount": 10},
            "tracker": {"limitBreached": False, "realizedPl": 0, "unrealizedPl": 0},
            "balance": {"tradableBalance": 10000},
            "open_positions": 0,
            "allocation": None,
            "active_tickers": 1,
            "strategy": None,
            "strategy_positions": 0,
        }
        self.d.update(overrides)
        self.calls: list[str] = []

    async def get_ticker(self, ticker_id): return self.d["ticker"]
    async def get_provider(self, provider_id): return self.d["provider"]
    async def get_schedule(self, provider_id): return self.d["schedule"]
    async def get_risk_limit(self, provider_id): return self.d["risk_limit"]
    async def get_today_tracker(self, provider_id, today): return self.d["tracker"]
    async def get_latest_balance(self, provider_id): return self.d["balance"]
    async def create_today_tracker(self, provider_id, today, cum_base): self.calls.append("create_tracker")
    async def mark_breached(self, provider_id, today): self.calls.append("mark_breached")
    async def create_alert(self, *a): self.calls.append("create_alert")
    async def flatten_all_positions(self, provider_id, reason): self.calls.append("flatten")
    async def count_open_positions(self, ticker_id): return self.d["open_positions"]
    async def get_latest_allocation(self, provider_id, ticker_id): return self.d["allocation"]
    async def count_active_tickers(self, provider_id): return self.d["active_tickers"]
    async def get_strategy(self, strategy_id): return self.d["strategy"]
    async def count_positions_for_strategy(self, strategy_id): return self.d["strategy_positions"]


def _intent(strategy_id=None):
    return OrderIntent(ticker_id="t1", side="long", price=100, strategy_id=strategy_id)


# ---- the four spec scenarios ----
async def test_approves_when_checks_pass():
    r = await evaluate_order_risk_gate(FakeStore(), _intent())
    assert r["approved"] is True
    assert r["isProbation"] is True
    # 10000 even-share x 25% probation / price 100 = 25.
    assert r["allowedQuantity"] == 25.0


async def test_rejects_when_max_concurrent_reached():
    r = await evaluate_order_risk_gate(FakeStore(open_positions=1), _intent())
    assert r["approved"] is False
    assert "Max concurrent positions limit reached" in r["reason"]


async def test_rejects_when_kill_switch_active():
    store = FakeStore(provider={"id": "p1", "tradingEnabled": True, "killSwitchActive": True, "killSwitchReason": "test breach"})
    r = await evaluate_order_risk_gate(store, _intent())
    assert r["approved"] is False
    assert "Kill switch active" in r["reason"]


async def test_rejects_when_trading_disabled():
    store = FakeStore(provider={"id": "p1", "tradingEnabled": False, "killSwitchActive": False})
    r = await evaluate_order_risk_gate(store, _intent())
    assert r["approved"] is False
    assert "Trading is disabled" in r["reason"]


# ---- sizing / probation ----
async def test_full_size_when_strategy_past_probation():
    store = FakeStore(strategy={"status": "live"}, strategy_positions=10)  # >= probationTradesCount
    r = await evaluate_order_risk_gate(store, _intent(strategy_id="s1"))
    assert r["isProbation"] is False
    assert r["allowedQuantity"] == 100.0  # full 10000 / 100


async def test_probation_when_strategy_under_trade_count():
    store = FakeStore(strategy={"status": "live"}, strategy_positions=3)
    r = await evaluate_order_risk_gate(store, _intent(strategy_id="s1"))
    assert r["isProbation"] is True
    assert r["allowedQuantity"] == 25.0


async def test_allocation_snapshot_overrides_even_share():
    store = FakeStore(allocation={"allocatedCapital": 4000}, strategy={"status": "live"}, strategy_positions=10)
    r = await evaluate_order_risk_gate(store, _intent(strategy_id="s1"))
    assert r["allowedQuantity"] == 40.0  # 4000 / 100, full size


# ---- daily-loss breach ----
async def test_halts_when_limit_already_breached():
    store = FakeStore(tracker={"limitBreached": True, "realizedPl": 0, "unrealizedPl": 0})
    with pytest.raises(RiskGateHalt):
        await evaluate_order_risk_gate(store, _intent())


async def test_detects_breach_flattens_and_halts():
    # net loss -300 on 10000 base = 3% >= 2% limit -> breach path.
    store = FakeStore(tracker={"limitBreached": False, "realizedPl": -300, "unrealizedPl": 0})
    with pytest.raises(RiskGateHalt):
        await evaluate_order_risk_gate(store, _intent())
    assert store.calls == ["mark_breached", "create_alert", "flatten"]


async def test_loss_under_limit_does_not_breach():
    # 1% loss < 2% limit -> proceeds to approve.
    store = FakeStore(tracker={"limitBreached": False, "realizedPl": -100, "unrealizedPl": 0})
    r = await evaluate_order_risk_gate(store, _intent())
    assert r["approved"] is True
    assert "mark_breached" not in store.calls


async def test_rejects_order_whose_size_rounds_to_zero():
    """Regression: a balance sync replaced the $10k default with a near-empty live balance, the
    allocation job gave the ticker $9.30, and 25% of that against BTC rounded to 0.0000 units. The
    gate approved it, so a zero-quantity position was opened that could never gain or lose and
    rendered as 'Size 0 / NaN%'. A size that rounds away is not a trade."""
    store = FakeStore(allocation={"allocatedCapital": 9.29996646})
    res = await evaluate_order_risk_gate(store, OrderIntent("t1", "short", 64657.43, "s1"))
    assert res["approved"] is False
    assert res["allowedQuantity"] == 0
    assert "rounds to zero" in res["reason"]


async def test_allows_order_when_allocation_is_adequate():
    """The same path with a funded allocation still sizes normally."""
    store = FakeStore(allocation={"allocatedCapital": 10000.0})
    res = await evaluate_order_risk_gate(store, OrderIntent("t1", "short", 64657.43, "s1"))
    assert res["approved"] is True and res["allowedQuantity"] > 0
