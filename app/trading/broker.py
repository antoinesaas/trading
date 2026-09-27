"""Interface ``Broker`` : tout exécuteur d'ordres (simulé ou, un jour, réel) l'implémente.

Le moteur ne dépend que de cette interface. Dans cette version, seul ``PaperBroker``
(``is_live = False``) est accepté par ``create_broker`` et ``ensure_paper_broker``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from app.core.types import Candle
from app.trading.orders import Order, OrderRequest


class Broker(ABC):
    is_live: ClassVar[bool]

    @abstractmethod
    def submit_order(self, request: OrderRequest, now: datetime) -> Order:
        """Enregistre un ordre (exécuté lors du prochain ``process_bar``)."""

    @abstractmethod
    def cancel_order(self, order_id: str, reason: str) -> bool:
        """Annule un ordre en attente."""

    @abstractmethod
    def close_position(self, symbol: str, reason: str, now: datetime) -> Order | None:
        """Demande la clôture au marché de la position ouverte sur ``symbol``."""

    @abstractmethod
    def modify_stop(self, symbol: str, new_stop: float, now: datetime, reason: str) -> None:
        """Déplace le stop de la position (trailing stop)."""

    @abstractmethod
    def process_bar(self, candle: Candle) -> None:
        """Exécute les ordres en attente et les sorties (SL/TP) sur une bougie."""

    @abstractmethod
    def pending_orders(self, symbol: str | None = None) -> list[Order]:
        """Ordres en attente d'exécution."""
