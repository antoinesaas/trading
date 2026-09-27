"""Diffusion temps réel des événements du moteur vers le dashboard (WebSocket)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.events import EngineEvent


class Broadcaster:
    """Abonné du bus : copie chaque événement dans la file de chaque client connecté."""

    def __init__(self, max_queue: int = 500) -> None:
        self._max_queue = max_queue
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()

    def register(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._max_queue)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    @property
    def clients(self) -> int:
        return len(self._queues)

    def __call__(self, event: EngineEvent) -> None:
        message = event.to_message()
        for queue in list(self._queues):
            if queue.full():  # client lent : on sacrifie le plus ancien message
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(message)
