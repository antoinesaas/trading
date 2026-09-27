"""Bus d'événements : découple le moteur de la persistance et du temps réel."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.core.types import to_payload, utcnow

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    SIGNAL = "signal"
    ORDER = "order"
    POSITION_OPENED = "position_opened"
    POSITION_UPDATED = "position_updated"
    POSITION_CLOSED = "position_closed"
    TRADE = "trade"
    EQUITY = "equity"
    CANDLE = "candle"
    BOT = "bot"
    RISK = "risk"
    OPTIMIZATION = "optimization"


@dataclass(frozen=True, slots=True)
class EngineEvent:
    type: EventType
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=utcnow)

    def to_message(self) -> dict[str, Any]:
        return {"type": self.type.value, "timestamp": self.timestamp.isoformat(),
                "data": to_payload(self.payload)}


Listener = Callable[[EngineEvent], None]


class EventBus:
    """Diffusion synchronue ; une erreur d'un abonné n'interrompt jamais le moteur."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def publish(self, event_type: EventType, payload: dict[str, Any]) -> EngineEvent:
        event = EngineEvent(event_type, payload)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - un abonné défaillant ne doit pas arrêter le bot
                logger.exception("Erreur dans un abonné du bus d'événements (%s)", event_type)
        return event


class EventCollector:
    """Abonné qui conserve les événements en mémoire (backtests, tests)."""

    def __init__(self) -> None:
        self.events: list[EngineEvent] = []

    def __call__(self, event: EngineEvent) -> None:
        self.events.append(event)

    def of_type(self, event_type: EventType) -> list[EngineEvent]:
        return [e for e in self.events if e.type is event_type]
