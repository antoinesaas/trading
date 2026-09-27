"""Backtest : rejoue des bougies historiques dans EXACTEMENT le même moteur que le paper trading.

Aucune règle n'est dupliquée ici : ``build_trading_stack`` assemble les mêmes
``Strategy``, ``RiskManager``, ``PaperBroker`` et ``Portfolio`` que le bot temps réel.
Les résultats sont déterministes pour des données et paramètres identiques.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.backtest.metrics import PerformanceMetrics, compute_metrics
from app.core.events import EngineEvent, EventType
from app.core.types import Candle, to_payload
from app.engine.params import ParameterSet
from app.engine.stack import build_trading_stack
from app.market.timeframes import periods_per_year
from app.risk.risk_manager import RiskLimits
from app.trading.positions import Trade

logger = logging.getLogger(__name__)

# Les ordres simulés d'un backtest ne doivent pas se mêler aux logs du bot temps réel.
SILENT_LOGGER = logging.getLogger("app.backtest.simulation")
SILENT_LOGGER.propagate = False
SILENT_LOGGER.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float


@dataclass(frozen=True, slots=True)
class BacktestResult:
    symbol: str
    timeframe: str
    start: datetime
    end: datetime
    bars: int
    params: ParameterSet
    limits: RiskLimits
    metrics: PerformanceMetrics
    trades: list[Trade]
    equity_curve: list[EquityPoint]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat(), "bars": self.bars},
            "parameters": {"trading": self.params.model_dump(), "risk_limits": asdict(self.limits)},
            "metrics": asdict(self.metrics),
            "trades": to_payload(self.trades),
            "equity_curve": to_payload(self.equity_curve),
        }


def _validate(candles: Sequence[Candle]) -> None:
    if len(candles) < 2:
        raise ValueError("Au moins 2 bougies sont nécessaires pour un backtest")
    symbols = {c.symbol for c in candles}
    timeframes = {c.timeframe for c in candles}
    if len(symbols) != 1 or len(timeframes) != 1:
        raise ValueError("Un backtest porte sur un seul symbole et un seul timeframe")
    if any(b.open_time <= a.open_time for a, b in zip(candles, candles[1:], strict=False)):
        raise ValueError("Les bougies doivent être triées et sans doublon")


def run_backtest(candles: Sequence[Candle], params: ParameterSet, limits: RiskLimits,
                 initial_capital: float, *, currency: str = "USDT",
                 lookback_bars: int = 300, verbose: bool = False) -> BacktestResult:
    _validate(candles)
    stack = build_trading_stack(params, limits, initial_capital, currency=currency,
                                lookback_bars=lookback_bars, log=None if verbose else SILENT_LOGGER)
    curve: list[EquityPoint] = []

    def record(event: EngineEvent) -> None:
        if event.type is EventType.EQUITY:
            data = event.payload
            curve.append(EquityPoint(datetime.fromisoformat(data["timestamp"]), data["equity"]))

    stack.bus.subscribe(record)
    for candle in candles:
        stack.engine.on_bar_update(candle)
        stack.engine.on_bar_close(candle)

    last = candles[-1]
    for order in stack.broker.pending_orders():
        stack.broker.cancel_order(order.id, "Fin du backtest")
    stack.broker.force_close(last.symbol, last.close, last.close_time, "Fin du backtest")
    final_equity = stack.portfolio.equity()
    if curve:
        curve[-1] = EquityPoint(curve[-1].timestamp, final_equity)

    metrics = compute_metrics(stack.portfolio.trades, [p.equity for p in curve], initial_capital,
                              periods_per_year(last.timeframe))
    logger.debug("Backtest %s %s : %d trades, rendement %.2f%%", last.symbol, last.timeframe,
                 metrics.trades, metrics.total_return * 100)
    return BacktestResult(
        symbol=last.symbol, timeframe=last.timeframe, start=candles[0].open_time,
        end=last.close_time, bars=len(candles), params=params, limits=limits, metrics=metrics,
        trades=list(stack.portfolio.trades), equity_curve=curve,
    )
