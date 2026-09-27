from datetime import UTC, datetime, timedelta

import pytest

from app.core.types import Direction, Signal
from app.risk.money_management import (
    OpenRisk, PerformanceSnapshot, confidence_scale, kelly_fraction, risk_fraction,
)
from app.risk.risk_manager import AccountSnapshot, ExitParams, RiskManager
from tests.conftest import zero_cost_limits

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def signal(direction=Direction.LONG, price=100.0, atr=1.0):
    return Signal("BTCUSDT", direction, price, atr, NOW, "test", "unit")


def account(**kwargs):
    base = {"equity": 10_000.0, "available_cash": 10_000.0, "open_positions": 0, "now": NOW}
    return AccountSnapshot(**(base | kwargs))


def kwargs(**overrides):
    base = dict(confidence=1.0, min_confidence=0.6, hard_max=0.02, min_risk=0.0025, stats=PerformanceSnapshot(),
                drawdown=0.0, max_drawdown=0.10, kelly_multiplier=0.25, kelly_min_trades=20)
    return base | overrides


def test_kelly_and_confidence_scale():
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
    assert kelly_fraction(0.3, 1.0) < 0
    assert confidence_scale(0.6, 0.6) == pytest.approx(0.5)
    assert confidence_scale(1.0, 0.6) == pytest.approx(1.0)


def test_risk_fraction_is_capped_and_scaled():
    assert risk_fraction(0.05, **kwargs())[0] == pytest.approx(0.02)  # plafond dur
    assert risk_fraction(0.02, **kwargs(confidence=0.6))[0] == pytest.approx(0.01)  # confiance minimale
    assert risk_fraction(0.01, **kwargs(drawdown=0.05))[0] == pytest.approx(0.01 * 0.625)
    losing = PerformanceSnapshot(trades=5, consecutive_losses=3)
    assert risk_fraction(0.01, **kwargs(stats=losing))[0] == pytest.approx(0.005)
    no_edge = PerformanceSnapshot(trades=30, win_rate=0.3, payoff_ratio=1.0)
    assert risk_fraction(0.01, **kwargs(stats=no_edge))[0] == pytest.approx(0.0025)  # Kelly négatif


def test_performance_snapshot_from_trades():
    class T:
        def __init__(self, pnl, hours):
            self.net_pnl, self.exit_time = pnl, NOW + timedelta(hours=hours)
    stats = PerformanceSnapshot.from_trades([T(100, 1), T(-50, 2), T(-50, 3)])
    assert stats.trades == 3 and stats.consecutive_losses == 2
    assert stats.win_rate == pytest.approx(1 / 3) and stats.payoff_ratio == pytest.approx(2.0)
    assert stats.last_loss_time == NOW + timedelta(hours=3)


def test_portfolio_heat_and_correlation_caps():
    rm = RiskManager(zero_cost_limits(max_portfolio_risk=0.05, max_correlated_risk=0.03), ExitParams())
    capped = rm.evaluate(signal(), account(open_risk=OpenRisk(total=0.025, long=0.025)))
    assert capped.approved and capped.risk_fraction == pytest.approx(0.005)
    assert not rm.evaluate(signal(), account(open_risk=OpenRisk(total=0.05, long=0.03))).approved
    short = rm.evaluate(signal(Direction.SHORT), account(open_risk=OpenRisk(total=0.03, long=0.03)))
    assert short.approved and short.risk_fraction == pytest.approx(0.01)


def test_cooldown_after_consecutive_losses():
    rm = RiskManager(zero_cost_limits(max_consecutive_losses=3, cooldown_hours=6), ExitParams())
    stats = PerformanceSnapshot(trades=3, consecutive_losses=3, last_loss_time=NOW - timedelta(hours=1))
    assert "Pause" in rm.evaluate(signal(), account(stats=stats)).reason
    later = account(stats=stats, now=NOW + timedelta(hours=6))
    assert rm.evaluate(signal(), later).approved


def test_weekly_loss_limit():
    rm = RiskManager(zero_cost_limits(max_daily_loss=0.5, max_weekly_loss=0.06), ExitParams())
    monday = datetime(2026, 9, 28, 1, tzinfo=UTC)
    rm.update_equity(monday, 10_000, 10_000)
    rm.update_equity(monday + timedelta(days=2), 9_300, 10_000)
    assert "hebdomadaire" in rm.evaluate(signal(), account()).reason
    rm.update_equity(monday + timedelta(days=7), 9_300, 10_000)  # nouvelle semaine
    assert rm.evaluate(signal(), account()).approved


def test_ai_levels_are_validated():
    rm = RiskManager(zero_cost_limits(min_reward_risk=1.5, min_stop_atr=0.5, max_stop_atr=6), ExitParams())
    ok = rm.evaluate(signal(), account(), stop_loss=98.0, take_profit=104.0, requested_risk=0.01, confidence=0.9)
    assert ok.approved and ok.stop_loss == 98.0 and ok.quantity == pytest.approx(50.0)
    assert "mauvais côté" in rm.evaluate(signal(), account(), stop_loss=101.0, take_profit=104.0).reason
    assert "ATR" in rm.evaluate(signal(), account(), stop_loss=99.9, take_profit=104.0).reason
    assert "Rendement/risque" in rm.evaluate(signal(), account(), stop_loss=98.0, take_profit=101.0).reason
    assert "ATR" in rm.evaluate(signal(), account(), stop_loss=90.0, take_profit=130.0).reason
