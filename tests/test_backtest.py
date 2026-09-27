from dataclasses import replace

import pytest

from app.backtest.engine import run_backtest
from app.backtest.metrics import compute_metrics, max_drawdown, sharpe_ratio
from app.backtest.report import format_summary, write_reports
from app.core.types import Direction, OrderType, Side
from app.engine.params import ParameterSet
from app.market.data_provider import generate_synthetic_candles
from app.risk.risk_manager import RiskLimits
from app.trading.positions import Trade
from tests.conftest import T0


@pytest.fixture(scope="module")
def candles():
    return generate_synthetic_candles(bars=1_500, seed=7)


@pytest.fixture(scope="module")
def result(candles):
    return run_backtest(candles, ParameterSet().validated(), RiskLimits(), 10_000.0)


def test_backtest_is_deterministic(candles, result):
    again = run_backtest(candles, ParameterSet().validated(), RiskLimits(), 10_000.0)
    assert again.metrics == result.metrics
    assert [t.id for t in again.trades] == [t.id for t in result.trades]


def test_backtest_accounting_is_consistent(result):
    m = result.metrics
    assert m.trades == len(result.trades) > 0
    assert m.final_equity == pytest.approx(10_000.0 + sum(t.net_pnl for t in result.trades))
    assert m.fees_paid == pytest.approx(sum(t.fees for t in result.trades))
    assert 0 <= m.win_rate <= 1 and 0 <= m.max_drawdown < 1


def test_backtest_report_contains_required_fields(result, tmp_path):
    data = result.to_dict()
    assert data["symbol"] == "SYNTHUSDT" and data["timeframe"] == "1h"
    assert {"start", "end", "bars"} <= data["period"].keys()
    assert {"trading", "risk_limits"} <= data["parameters"].keys()
    for key in ("total_return", "trades", "win_rate", "avg_win", "avg_loss", "profit_factor", "expectancy",
                "max_drawdown", "sharpe_ratio"):
        assert key in data["metrics"]
    assert len(data["equity_curve"]) == result.bars
    paths = write_reports(result, tmp_path, "USDT")
    assert all(p.exists() for p in paths.values())
    assert "Rendement total" in format_summary(result, "USDT")


def test_backtest_uses_risk_limits(candles):
    risky = run_backtest(candles, ParameterSet().validated(), RiskLimits(risk_per_trade=0.02), 10_000.0)
    base = run_backtest(candles, ParameterSet().validated(), RiskLimits(risk_per_trade=0.01), 10_000.0)
    assert risky.trades[0].quantity > base.trades[0].quantity


def _trade(pnl: float, r: float = 1.0) -> Trade:
    return Trade("TR-1", 1, "POS-1", "X", Direction.LONG, Side.BUY, OrderType.TAKE_PROFIT, T0, T0, 100.0, 101.0,
                 1.0, 99.0, 102.0, 0.5, 0.0, pnl + 0.5, pnl, 0.01, r, "t", "r")


def test_metrics_on_known_trades():
    trades = [_trade(200, 2), _trade(-100, -1), _trade(100, 1), _trade(-100, -1)]
    m = compute_metrics(trades, [10_000, 10_200, 10_100], 10_000, 365 * 24)
    assert m.win_rate == 0.5
    assert m.avg_win == 150 and m.avg_loss == -100
    assert m.profit_factor == pytest.approx(1.5)
    assert m.expectancy == pytest.approx(25.0)
    assert m.expectancy_r == pytest.approx(0.25)
    assert m.sharpe_ratio is None  # trop peu de points


def test_profit_factor_undefined_without_losses():
    assert compute_metrics([_trade(10)], [10_010], 10_000, 1).profit_factor is None


def test_max_drawdown_and_sharpe():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert sharpe_ratio([100.0] * 40, 365) is None
    rising = [100 * (1.001 ** i) + (i % 2) * 0.01 for i in range(60)]
    assert sharpe_ratio(rising, 365) > 0


def test_backtest_rejects_mixed_or_unsorted_data(candles):
    with pytest.raises(ValueError):
        run_backtest(list(reversed(candles[:10])), ParameterSet(), RiskLimits(), 10_000)
    with pytest.raises(ValueError):
        run_backtest([candles[0], replace(candles[1], symbol="OTHER")], ParameterSet(), RiskLimits(), 10_000)
