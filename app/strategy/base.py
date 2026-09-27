"""Contrat commun à toutes les stratégies.

Une stratégie ne fait QUE lire des bougies clôturées et émettre un ``Signal``.
Elle ne connaît ni le capital, ni les ordres, ni le broker : taille de position,
stop-loss et take-profit relèvent du ``RiskManager``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict

from app.core.types import Candle, Signal
from app.market.indicators import FloatArray


class StrategyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class CandleWindow:
    """Fenêtre de bougies clôturées sous forme de tableaux NumPy."""

    symbol: str
    timeframe: str
    open_time: tuple[datetime, ...]
    close_time: tuple[datetime, ...]
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray

    @classmethod
    def from_candles(cls, candles: Sequence[Candle]) -> CandleWindow:
        if not candles:
            raise ValueError("Fenêtre vide")
        return cls(
            symbol=candles[0].symbol,
            timeframe=candles[0].timeframe,
            open_time=tuple(c.open_time for c in candles),
            close_time=tuple(c.close_time for c in candles),
            open=np.fromiter((c.open for c in candles), np.float64, len(candles)),
            high=np.fromiter((c.high for c in candles), np.float64, len(candles)),
            low=np.fromiter((c.low for c in candles), np.float64, len(candles)),
            close=np.fromiter((c.close for c in candles), np.float64, len(candles)),
        )

    def __len__(self) -> int:
        return self.close.size


@dataclass(frozen=True, slots=True)
class Analysis:
    signal: Signal | None
    atr: float | None
    indicators: dict[str, float] = field(default_factory=dict)


class Strategy(ABC):
    name: ClassVar[str]
    params_model: ClassVar[type[StrategyParams]]
    # Espace de recherche proposé à l'optimiseur : {paramètre: (min, max)}.
    search_space: ClassVar[dict[str, tuple[float, float]]]

    def __init__(self, params: StrategyParams | None = None) -> None:
        self.params = params if params is not None else self.params_model()
        if not isinstance(self.params, self.params_model):
            raise TypeError(f"{self.name} attend des paramètres {self.params_model.__name__}")

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Nombre minimal de bougies avant de pouvoir émettre un signal fiable."""

    @abstractmethod
    def analyze(self, window: CandleWindow) -> Analysis:
        """Analyse la DERNIÈRE bougie clôturée de la fenêtre."""

    @abstractmethod
    def indicator_series(self, window: CandleWindow) -> dict[str, FloatArray]:
        """Séries d'indicateurs à superposer sur le graphique."""
