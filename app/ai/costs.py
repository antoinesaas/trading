"""Suivi et plafonnement du coût des appels à l'API Claude.

Tarifs publics par million de tokens (entrée, sortie, lecture de cache) ; l'écriture de
cache est facturée 1,25 x l'entrée. Recherche web : 10 $ pour 1 000 recherches.
Un modèle inconnu est facturé au tarif Opus (hypothèse prudente).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
WEB_SEARCH_PRICE = 0.01


class BudgetExceededError(RuntimeError):
    """Budget quotidien ou limite horaire d'appels atteints."""


@dataclass(frozen=True, slots=True)
class UsageRecord:
    timestamp: datetime
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    web_searches: int
    cost_usd: float


def cost_of(model: str, usage: Any) -> tuple[float, dict[str, int]]:
    price_in, price_out, price_cache = PRICES.get(model, PRICES["claude-opus-5"])
    counts = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "web_searches": int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0),
    }
    cost = (counts["input_tokens"] * price_in + counts["output_tokens"] * price_out
            + counts["cache_read_tokens"] * price_cache + counts["cache_write_tokens"] * price_in * 1.25) / 1e6
    return cost + counts["web_searches"] * WEB_SEARCH_PRICE, counts


class CostTracker:
    """Budget quotidien (UTC) et nombre maximal d'appels par heure, persistés via ``sink``."""

    def __init__(self, daily_budget_usd: float, max_calls_per_hour: int, *, spent_today: float = 0.0,
                 sink: Callable[[UsageRecord], None] | None = None) -> None:
        self.daily_budget = daily_budget_usd
        self.max_calls_per_hour = max_calls_per_hour
        self._sink = sink
        self._lock = threading.Lock()
        self._day = datetime.now(UTC).date()
        self._spent = spent_today
        self._calls: deque[datetime] = deque()

    def check(self, estimate_usd: float, purpose: str) -> None:
        with self._lock:
            now = datetime.now(UTC)
            self._roll(now)
            while self._calls and now - self._calls[0] > timedelta(hours=1):
                self._calls.popleft()
            if len(self._calls) >= self.max_calls_per_hour:
                raise BudgetExceededError(f"{purpose} : limite de {self.max_calls_per_hour} appels/heure atteinte")
            if self._spent + estimate_usd > self.daily_budget:
                raise BudgetExceededError(
                    f"{purpose} : budget IA du jour atteint ({self._spent:.2f} / {self.daily_budget:.2f} $)")
            self._calls.append(now)

    def record(self, purpose: str, model: str, usage: Any) -> UsageRecord:
        cost, counts = cost_of(model, usage)
        record = UsageRecord(timestamp=datetime.now(UTC), purpose=purpose, model=model, cost_usd=cost, **counts)
        with self._lock:
            self._roll(record.timestamp)
            self._spent += cost
        logger.info("Claude %s (%s) : %d in / %d out, %d recherche(s), %.4f $ (jour : %.2f $)", purpose, model,
                    counts["input_tokens"], counts["output_tokens"], counts["web_searches"], cost, self._spent)
        if self._sink is not None:
            self._sink(record)
        return record

    def _roll(self, now: datetime) -> None:
        if now.date() != self._day:
            self._day, self._spent = now.date(), 0.0

    def status(self) -> dict[str, float]:
        with self._lock:
            self._roll(datetime.now(UTC))
            return {"spent_today_usd": round(self._spent, 4), "daily_budget_usd": self.daily_budget,
                    "remaining_usd": round(max(0.0, self.daily_budget - self._spent), 4)}
