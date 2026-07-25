"""Unit tests for the Strategy Supervisor's deterministic guardrail math (Phase 3a).

Pure functions only — no DB, no Redis — so the regeneration decision logic is pinned down
independently of I/O."""
from worker import supervisor as sup


def test_profit_factor_matches_backend_fallbacks():
    assert sup.profit_factor([10, -5, 20, -5]) == 3.0        # gp 30 / gl 10
    assert sup.profit_factor([10, 20]) == 3.0                # no losses, some profit -> 3.0
    assert sup.profit_factor([-10, -20]) == 0.0              # no profit -> 0.0
    assert sup.profit_factor([]) == 0.0


def test_divergence_pct():
    assert sup.divergence_pct(1.0, 2.0) == 50.0
    assert sup.divergence_pct(2.0, 2.0) == 0.0
    assert sup.divergence_pct(5.0, 0.0) == 100.0             # undefined baseline -> 100%


def test_consecutive_losses_counts_trailing_only():
    assert sup.consecutive_losses([10, -1, -2, -3]) == 3
    assert sup.consecutive_losses([-1, -2, 10]) == 0
    assert sup.consecutive_losses([0, 0]) == 2               # break-even counts as non-win


def test_max_drawdown_pct():
    # Raw cumulative-PnL drawdown (base 0): peak 100 -> trough 50 == 50%.
    assert sup.max_drawdown_pct([100, -50], 0.0) == 50.0
    assert sup.max_drawdown_pct([10, 20, 30], 0.0) == 0.0    # monotonically up
    assert sup.max_drawdown_pct([], 0.0) == 0.0
    # Against a notional base the same dip is a small fraction, not 50%.
    assert sup.max_drawdown_pct([100, -50], 10000.0) < 1.0


def test_win_rate_pct():
    assert sup.win_rate_pct([1, -1, 1, 1]) == 75.0
    assert sup.win_rate_pct([]) == 0.0


def test_evaluate_needs_minimum_trades():
    # Fewer than supervisor_min_closed_trades (default 5) -> never trips.
    assert sup.evaluate([-1, -1, -1, -1], backtest_profit_factor=2.0) is None


def test_evaluate_trips_on_divergence():
    # 5 trades, live PF ~0.67 vs backtest 2.0 -> ~66% divergence (>= 30% default).
    reason = sup.evaluate([10, -10, 10, -10, -10], backtest_profit_factor=2.0)
    assert reason is not None and "divergence" in reason


def test_evaluate_trips_on_consecutive_losses_when_divergence_ok():
    # Live PF == backtest PF (no divergence), but 5 trailing losses -> consecutive-loss trip.
    pls = [50, 50, 50, -10, -10, -10, -10, -10]  # gp 150 / gl 50 = PF 3.0
    reason = sup.evaluate(pls, backtest_profit_factor=3.0)
    assert reason is not None and "consecutive" in reason


def test_evaluate_healthy_strategy_does_not_trip():
    # Matching performance: gp 300 / gl 100 = PF 3.0 vs backtest 3.0 (no divergence), no long
    # loss streak, win rate 50%, and drawdown tiny against the notional base.
    pls = [100, -50, 100, -50, 100]
    assert sup.evaluate(pls, backtest_profit_factor=3.0) is None
