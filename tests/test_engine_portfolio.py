from datetime import timedelta

import pytest

from app.core.events import EventType
from app.core.types import Direction, OrderIntent, Signal
from app.trading.portfolio import Portfolio
from tests.conftest import T0, candle, zero_cost_limits


def make_signal(direction=Direction.LONG, price=100.0, atr=1.0):
    return Signal("TEST", direction, price, atr, T0, "test", "unit")


def test_portfolio_equity_and_drawdown():
    portfolio = Portfolio(10_000.0)
    assert portfolio.equity() == 10_000.0 and portfolio.drawdown() == 0.0
    portfolio.snapshot(T0)
    portfolio.balance = 9_500.0
    assert portfolio.drawdown() == pytest.approx(0.05)


def test_accepted_signal_creates_order_then_position(make_stack):
    stack = make_stack(zero_cost_limits())
    outcome = stack.engine.handle_signal(make_signal())
    assert outcome.status == "accepted" and outcome.order_id
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1))
    position = stack.portfolio.position("TEST")
    assert position.direction is Direction.LONG and position.quantity == 50.0


def test_paused_engine_ignores_signals(make_stack):
    stack = make_stack(zero_cost_limits())
    stack.engine.entries_enabled = False
    outcome = stack.engine.handle_signal(make_signal())
    assert outcome.status == "ignored"
    assert stack.broker.pending_orders() == []


def test_same_direction_signal_ignored_and_opposite_reverses(make_stack):
    stack = make_stack(zero_cost_limits())
    stack.engine.handle_signal(make_signal())
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1))
    assert stack.engine.handle_signal(make_signal()).status == "ignored"

    outcome = stack.engine.handle_signal(make_signal(Direction.SHORT))
    assert outcome.status == "accepted"
    intents = [o.intent for o in stack.broker.pending_orders()]
    assert intents == [OrderIntent.CLOSE, OrderIntent.OPEN]
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=2))
    assert stack.portfolio.position("TEST").direction is Direction.SHORT
    assert stack.portfolio.trades[-1].reason == "Signal opposé"


def test_signal_rejected_by_risk_manager_is_recorded(make_stack, collector):
    stack = make_stack(zero_cost_limits(max_open_positions=1))
    stack.engine.handle_signal(make_signal())
    outcome = stack.engine.handle_signal(Signal("OTHER", Direction.LONG, 50.0, 1.0, T0, "test", "unit"))
    assert outcome.status == "rejected"
    statuses = [e.payload["status"] for e in collector.of_type(EventType.SIGNAL)]
    assert statuses == ["accepted", "rejected"]


def test_daily_loss_limit_blocks_new_entries(make_stack):
    stack = make_stack(zero_cost_limits(max_daily_loss=0.03))
    stack.risk.update_equity(T0, 10_000.0, 10_000.0)
    stack.risk.update_equity(T0 + timedelta(hours=1), 9_600.0, 10_000.0)
    outcome = stack.engine.handle_signal(make_signal())
    assert outcome.status == "rejected" and "journalière" in outcome.reason


def test_max_drawdown_kill_switch_closes_positions(make_stack, collector):
    stack = make_stack(zero_cost_limits(max_drawdown=0.10, risk_per_trade=0.05))
    stack.engine.handle_signal(make_signal(atr=10.0))
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1))
    position = stack.portfolio.position("TEST")
    assert position is not None
    stack.portfolio.balance -= 1_500.0  # perte simulée sur un autre compte-rendu
    stack.engine.on_bar_close(candle(100, 100.5, 99.5, 100, index=1))
    assert stack.risk.halted
    assert any(e.payload["kind"] == "max_drawdown" for e in collector.of_type(EventType.RISK))
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=2))
    assert stack.portfolio.position("TEST") is None
    assert stack.engine.handle_signal(make_signal()).status == "rejected"
