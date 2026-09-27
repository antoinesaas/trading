"""Types de domaine partagés par tous les composants."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is Direction.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self is Direction.LONG else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OrderIntent(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class SignalSource(StrEnum):
    INTERNAL = "internal"
    TRADINGVIEW = "tradingview"


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    direction: Direction
    price: float
    atr: float
    timestamp: datetime
    strategy: str
    reason: str
    source: SignalSource = SignalSource.INTERNAL
    indicators: dict[str, float] = field(default_factory=dict)


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalise une date en UTC (les dates naïves sont supposées UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def to_payload(obj: Any) -> Any:
    """Sérialise dataclasses / enums / dates en structures JSON-compatibles."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_payload(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_payload(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_payload(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, float) and obj != obj:  # NaN -> null
        return None
    return obj
