"""Ordres simulés et génération d'identifiants uniques."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.types import Direction, OrderIntent, OrderStatus, OrderType, Side


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: float
    order_type: OrderType = OrderType.MARKET
    intent: OrderIntent = OrderIntent.OPEN
    limit_price: float | None = None
    reference_price: float | None = None
    stop_distance: float | None = None
    take_profit_distance: float | None = None
    tp1_distance: float | None = None
    tp1_fraction: float = 0.0
    breakeven_at_r: float = 0.0
    time_stop: timedelta | None = None
    decision_id: int | None = None
    confidence: float | None = None
    strategy: str = ""
    reason: str = ""

    @property
    def direction(self) -> Direction:
        return Direction.LONG if self.side is Side.BUY else Direction.SHORT


@dataclass(slots=True)
class Order:
    id: str
    seq: int
    symbol: str
    side: Side
    order_type: OrderType
    intent: OrderIntent
    quantity: float
    status: OrderStatus
    created_at: datetime
    strategy: str = ""
    reason: str = ""
    limit_price: float | None = None
    reference_price: float | None = None
    stop_distance: float | None = None
    take_profit_distance: float | None = None
    fill_price: float | None = None
    filled_at: datetime | None = None
    fees: float = 0.0
    slippage: float = 0.0
    position_id: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reject_reason: str | None = None
    tp1_distance: float | None = None
    tp1_fraction: float = 0.0
    breakeven_at_r: float = 0.0
    time_stop: timedelta | None = None
    decision_id: int | None = None
    confidence: float | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is OrderStatus.PENDING


class IdGenerator:
    """Identifiants séquentiels déterministes (``PO-000142``) : reproductibles en backtest."""

    def __init__(self, start: dict[str, int] | None = None) -> None:
        self._counters: dict[str, int] = dict(start or {})

    def next(self, prefix: str) -> tuple[str, int]:
        seq = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = seq
        return f"{prefix}-{seq:06d}", seq


def apply_slippage(price: float, side: Side, rate: float) -> float:
    """Prix d'exécution défavorable : un achat paie plus cher, une vente reçoit moins."""
    return price * (1 + rate) if side is Side.BUY else price * (1 - rate)
