"""Assemblage unique des composants de trading (backtest ET temps réel)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.events import EventBus
from app.engine.params import ParameterSet
from app.engine.trading_engine import TradingEngine
from app.risk.risk_manager import RiskLimits, RiskManager
from app.trading.factory import create_broker
from app.trading.orders import IdGenerator
from app.trading.paper_broker import PaperBroker
from app.trading.portfolio import Portfolio


@dataclass(slots=True)
class TradingStack:
    engine: TradingEngine
    broker: PaperBroker
    portfolio: Portfolio
    risk: RiskManager
    bus: EventBus


def build_trading_stack(params: ParameterSet, limits: RiskLimits, initial_capital: float, *,
                        bus: EventBus | None = None, portfolio: Portfolio | None = None,
                        ids: IdGenerator | None = None, currency: str = "USDT",
                        lookback_bars: int = 300, internal_signals: bool = True,
                        log: logging.Logger | None = None) -> TradingStack:
    bus = bus or EventBus()
    portfolio = portfolio or Portfolio(initial_capital)
    risk = RiskManager(limits, params.exits, currency, log)
    broker = create_broker("paper", portfolio, bus, limits, ids, log)
    engine = TradingEngine(params.build_strategy(), risk, broker, portfolio, bus,
                           lookback_bars=lookback_bars, internal_signals=internal_signals, log=log)
    return TradingStack(engine, broker, portfolio, risk, bus)
