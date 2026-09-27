"""Métriques de performance (partagées par le backtest et le dashboard).

Toutes les valeurs relatives sont des fractions (0.12 = 12 %).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from app.trading.positions import Trade

MIN_POINTS_FOR_SHARPE = 30


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    initial_capital: float
    final_equity: float
    net_profit: float
    total_return: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None  # None = pas de perte (infini) ou aucun trade
    expectancy: float
    expectancy_r: float
    max_drawdown: float
    sharpe_ratio: float | None  # None = pas assez de points ou volatilité nulle
    fees_paid: float


def max_drawdown(equity: Sequence[float]) -> float:
    values = np.asarray(equity, dtype=np.float64)
    if values.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(values)
    return float(np.max((peaks - values) / peaks))


def sharpe_ratio(equity: Sequence[float], periods_per_year: float) -> float | None:
    """Sharpe annualisé des rendements par bougie (taux sans risque = 0)."""
    values = np.asarray(equity, dtype=np.float64)
    if values.size < MIN_POINTS_FOR_SHARPE:
        return None
    returns = np.diff(values) / values[:-1]
    std = returns.std(ddof=1)
    if not math.isfinite(std) or std == 0:
        return None
    return float(returns.mean() / std * math.sqrt(periods_per_year))


def compute_metrics(trades: Sequence[Trade], equity: Sequence[float], initial_capital: float,
                    periods_per_year: float) -> PerformanceMetrics:
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    final_equity = equity[-1] if equity else initial_capital
    return PerformanceMetrics(
        initial_capital=initial_capital,
        final_equity=final_equity,
        net_profit=final_equity - initial_capital,
        total_return=final_equity / initial_capital - 1,
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        avg_win=gross_profit / len(wins) if wins else 0.0,
        avg_loss=-gross_loss / len(losses) if losses else 0.0,
        profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
        expectancy=sum(pnls) / len(pnls) if pnls else 0.0,
        expectancy_r=sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0,
        max_drawdown=max_drawdown([initial_capital, *equity]),
        sharpe_ratio=sharpe_ratio(equity, periods_per_year),
        fees_paid=sum(t.fees for t in trades),
    )
