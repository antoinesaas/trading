from datetime import UTC, datetime, timedelta

import pytest

from app.core.types import Direction, Signal
from app.risk.risk_manager import (
    AccountSnapshot, ExitParams, RiskLimits, RiskManager, compute_position_size, compute_stop_loss,
    compute_take_profit, round_down_to_step, trailing_stop_level,
)
from app.trading.positions import Position
from tests.conftest import zero_cost_limits

NOW = datetime(2025, 1, 1, 12, tzinfo=UTC)


def signal(direction=Direction.LONG, price=100.0, atr=1.0, symbol="BTCUSDT"):
    return Signal(symbol, direction, price, atr, NOW, "test", "unit test")


def account(equity=10_000.0, cash=10_000.0, open_positions=0):
    return AccountSnapshot(equity, cash, open_positions)


# --- Taille de position -------------------------------------------------------------------

def test_position_size_matches_specification_example():
    # Capital 10 000, risque 1 % = 100 ; stop à 2 du prix d'entrée => 50 unités.
    assert compute_position_size(10_000, 0.01, 100.0, 98.0, qty_step=0.0001) == 50.0


def test_position_size_rounds_down_to_step():
    assert compute_position_size(10_000, 0.01, 100.0, 97.0, qty_step=0.001) == 33.333
    assert compute_position_size(10_000, 0.01, 100.0, 97.0, qty_step=1) == 33.0
    assert round_down_to_step(0.30000000000000004, 0.1) == pytest.approx(0.3)


def test_position_size_includes_fees_and_slippage():
    with_costs = compute_position_size(10_000, 0.01, 100.0, 98.0, 0.0001, fee_rate=0.001, slippage_rate=0.0005)
    assert with_costs < 50.0
    # Perte réelle au stop <= budget de risque.
    loss = with_costs * (2.0 + 100 * 0.001 + 98 * 0.0015)
    assert loss <= 100.0 + 1e-9


# --- Stop-loss et take-profit ------------------------------------------------------------------

def test_stop_loss_long_and_short():
    assert compute_stop_loss(Direction.LONG, 100.0, 1.5, 2.0) == pytest.approx(97.0)
    assert compute_stop_loss(Direction.SHORT, 100.0, 1.5, 2.0) == pytest.approx(103.0)


def test_take_profit_long_and_short():
    assert compute_take_profit(Direction.LONG, 100.0, 97.0, 2.0) == pytest.approx(106.0)
    assert compute_take_profit(Direction.SHORT, 100.0, 103.0, 2.0) == pytest.approx(94.0)


def test_invalid_stop_raises():
    with pytest.raises(ValueError):
        compute_stop_loss(Direction.LONG, 10.0, 6.0, 2.0)  # stop négatif
    with pytest.raises(ValueError):
        compute_stop_loss(Direction.LONG, 100.0, 0.0, 2.0)


# --- Décisions du Risk Manager ----------------------------------------------------------------

def test_evaluate_approves_with_expected_levels():
    rm = RiskManager(zero_cost_limits(), ExitParams(stop_loss_atr_multiplier=2, take_profit_risk_reward=2))
    decision = rm.evaluate(signal(price=100.0, atr=1.0), account())
    assert decision.approved
    assert decision.quantity == 50.0
    assert decision.stop_loss == pytest.approx(98.0)
    assert decision.take_profit == pytest.approx(104.0)
    assert decision.risk_amount == pytest.approx(100.0)


def test_evaluate_rejects_when_max_positions_reached():
    rm = RiskManager(zero_cost_limits(max_open_positions=3), ExitParams())
    decision = rm.evaluate(signal(), account(open_positions=3))
    assert not decision.approved and "maximal" in decision.reason


def test_evaluate_rejects_below_minimum_size():
    rm = RiskManager(zero_cost_limits(min_qty=1.0, qty_step=1.0), ExitParams())
    decision = rm.evaluate(signal(price=50_000.0, atr=2_000.0), account())
    assert not decision.approved and "minimum" in decision.reason


def test_evaluate_caps_quantity_by_available_cash():
    rm = RiskManager(zero_cost_limits(), ExitParams())
    decision = rm.evaluate(signal(price=100.0, atr=0.01), account(cash=1_000.0))
    assert decision.approved and decision.quantity == pytest.approx(10.0)


def test_evaluate_rejects_invalid_signal_values():
    rm = RiskManager(zero_cost_limits(), ExitParams())
    assert not rm.evaluate(signal(atr=float("nan")), account()).approved
    assert not rm.evaluate(signal(price=-1.0), account()).approved


# --- Limites de compte ---------------------------------------------------------------------------

def test_daily_loss_limit_blocks_entries_until_next_day():
    rm = RiskManager(zero_cost_limits(max_daily_loss=0.03), ExitParams())
    rm.update_equity(NOW, 10_000.0, 10_000.0)
    alerts = rm.update_equity(NOW + timedelta(hours=1), 9_690.0, 10_000.0)
    assert [a.kind for a in alerts] == ["daily_loss"]
    assert not rm.evaluate(signal(), account(equity=9_690)).approved
    rm.update_equity(NOW + timedelta(days=1), 9_690.0, 10_000.0)
    assert rm.evaluate(signal(), account(equity=9_690)).approved


def test_max_drawdown_triggers_kill_switch():
    rm = RiskManager(zero_cost_limits(max_drawdown=0.10), ExitParams())
    alerts = rm.update_equity(NOW, 8_990.0, 10_000.0)
    assert "max_drawdown" in [a.kind for a in alerts]
    assert rm.halted
    decision = rm.evaluate(signal(), account(equity=8_990))
    assert not decision.approved and "Kill-switch" in decision.reason
    rm.reset_halt()
    assert not rm.halted


def test_trailing_stop_only_moves_in_favour_after_activation():
    position = Position("POS-1", 1, "X", Direction.LONG, 1.0, 100.0, NOW, 98.0, 104.0, 98.0, 0.0, 0.0, "t", "PO-1")
    exits = ExitParams(trailing_stop_enabled=True, trailing_stop_atr_multiplier=1.0, trailing_activation_r=1.0)
    assert trailing_stop_level(position, 101.0, 1.0, exits) is None  # +1 < 1R (2)
    assert trailing_stop_level(position, 103.0, 1.0, exits) == pytest.approx(102.0)
    position.stop_loss = 102.5
    assert trailing_stop_level(position, 103.0, 1.0, exits) is None  # n'améliore pas le stop


def test_limits_are_frozen_dataclass():
    with pytest.raises(AttributeError):
        RiskLimits().risk_per_trade = 0.5  # type: ignore[misc]
