from datetime import timedelta

import pytest

from app.core.types import Direction, OrderStatus, OrderType, Side, Signal
from app.engine.trading_engine import TradePlan
from app.risk.risk_manager import ExitParams
from app.engine.params import ParameterSet
from app.trading.orders import OrderRequest
from tests.conftest import T0, candle, zero_cost_limits


def open_long(stack, plan=None, price=100.0):
    signal = Signal("TEST", Direction.LONG, price, 1.0, T0, "test", "unit")
    outcome = stack.engine.open_trade(signal, plan or TradePlan(stop_loss=98.0, take_profit=104.0), T0)
    assert outcome.status == "accepted", outcome.reason
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1))
    return stack.portfolio.position("TEST")


def test_partial_take_profit_then_breakeven_and_blended_trade(make_stack):
    stack = make_stack(zero_cost_limits())
    position = open_long(stack, TradePlan(stop_loss=98.0, take_profit=104.0, tp1_price=102.0, tp1_fraction=0.5))
    assert position.tp1_price == pytest.approx(102.0) and position.quantity == 50
    stack.engine.on_bar_update(candle(100.5, 102.5, 100.2, 102.2, index=2))
    assert position.tp1_done and position.quantity == 25 and position.stop_loss == pytest.approx(100.0)
    assert stack.portfolio.balance == pytest.approx(10_000 + 25 * 2.0)
    stack.engine.on_bar_update(candle(101, 101.2, 99.5, 99.8, index=3))  # retour au point mort
    trade = stack.portfolio.trades[-1]
    assert trade.quantity == 50 and trade.exit_price == pytest.approx(101.0)
    assert trade.net_pnl == pytest.approx(50.0) and "TP1" in trade.reason
    assert stack.portfolio.balance == pytest.approx(10_050.0)


def test_breakeven_and_time_stop_from_default_exits(make_stack):
    params = ParameterSet(exits=ExitParams(breakeven_at_r=1.0, time_stop_bars=3))
    stack = make_stack(zero_cost_limits(), params=params)
    signal = Signal("TEST", Direction.LONG, 100.0, 1.0, T0, "test", "unit")
    stack.engine.handle_signal(signal)
    stack.engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1))
    stack.engine.on_bar_close(candle(100, 100.5, 99.5, 100, index=1))
    position = stack.portfolio.position("TEST")
    assert position.time_stop_at == T0 + timedelta(hours=1) + timedelta(hours=3)
    stack.engine.on_bar_close(candle(100, 102.5, 100, 102.2, index=2))  # +1,1 R
    assert position.breakeven_done and position.stop_loss == pytest.approx(100.0)
    for i in (3, 4):
        stack.engine.on_bar_update(candle(100.4, 100.6, 100.2, 100.3, index=i))
        stack.engine.on_bar_close(candle(100.4, 100.6, 100.2, 100.3, index=i))
    stack.engine.on_bar_update(candle(100.3, 100.5, 100.2, 100.4, index=5))
    assert stack.portfolio.position("TEST") is None
    assert "Time-stop" in stack.portfolio.trades[-1].reason


def test_ai_adjustments_can_only_tighten_the_stop(make_stack):
    stack = make_stack(zero_cost_limits())
    position = open_long(stack)
    assert "refusé" in stack.engine.manage_position("TEST", T0, new_stop=97.0)
    assert "stop ->" in stack.engine.manage_position("TEST", T0, new_stop=99.0)
    assert position.stop_loss == 99.0 and position.stop_reason.startswith("Stop ajusté")
    assert "objectif ->" in stack.engine.manage_position("TEST", T0, new_target=106.0)
    assert stack.engine.manage_position("TEST", T0, close=True) == "Clôture demandée"


def test_mid_candle_order_fills_at_latest_price_and_ignores_prior_prices(make_stack):
    stack = make_stack(zero_cost_limits())
    created = T0 + timedelta(hours=1, minutes=20)
    stack.broker.submit_order(OrderRequest("TEST", Side.BUY, 10, stop_distance=2.0, take_profit_distance=4.0),
                              created)
    forming = candle(95, 101, 94, 100, index=1, closed=False)  # bougie ouverte avant l'ordre
    stack.broker.process_bar(forming)
    position = stack.portfolio.position("TEST")
    assert position.entry_price == pytest.approx(100.0) and position.entry_time == created
    assert stack.portfolio.position("TEST") is not None  # le plus bas à 94 est antérieur : pas de stop
    stack.broker.process_bar(candle(95, 101, 94, 100, index=1))  # même bougie, clôturée
    assert stack.portfolio.position("TEST") is not None
    stack.broker.process_bar(candle(100, 100.2, 97.5, 98, index=2))
    assert stack.portfolio.trades[-1].order_type is OrderType.STOP_LOSS


def test_closed_candle_before_order_is_never_used(make_stack):
    stack = make_stack(zero_cost_limits())
    order = stack.broker.submit_order(OrderRequest("TEST", Side.BUY, 1, stop_distance=2.0), T0 + timedelta(minutes=5))
    stack.broker.process_bar(candle(50, 51, 49, 50, index=0))
    assert order.status is OrderStatus.PENDING
