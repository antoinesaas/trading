"""Stratégies disponibles. Pour en ajouter une : sous-classer ``Strategy`` et l'enregistrer ici."""

from app.strategy.base import Analysis, CandleWindow, Strategy, StrategyParams
from app.strategy.confluence_strategy import ConfluenceParams, ConfluenceStrategy
from app.strategy.ema_rsi_strategy import EmaRsiParams, EmaRsiStrategy

STRATEGIES: dict[str, type[Strategy]] = {EmaRsiStrategy.name: EmaRsiStrategy,
                                         ConfluenceStrategy.name: ConfluenceStrategy}

__all__ = ["STRATEGIES", "Analysis", "CandleWindow", "ConfluenceParams", "ConfluenceStrategy", "EmaRsiParams",
           "EmaRsiStrategy", "Strategy", "StrategyParams"]
