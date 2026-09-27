"""Positions ouvertes et trades clôturés."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.types import Candle, Direction, OrderType, Side


@dataclass(slots=True)
class Position:
    id: str
    seq: int
    symbol: str
    direction: Direction
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    initial_stop_loss: float
    entry_fee: float
    entry_slippage: float
    strategy: str
    entry_order_id: str
    entry_reason: str = ""
    trailing_active: bool = False
    last_price: float = 0.0

    def __post_init__(self) -> None:
        if not self.last_price:
            self.last_price = self.entry_price

    @property
    def side(self) -> Side:
        return self.direction.entry_side

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.quantity

    @property
    def initial_risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_stop_loss)

    def unrealized_pnl(self, price: float | None = None) -> float:
        mark = self.last_price if price is None else price
        return self.direction.sign * (mark - self.entry_price) * self.quantity

    def favorable_move(self, price: float) -> float:
        return self.direction.sign * (price - self.entry_price)

    def exit_trigger(self, candle: Candle) -> tuple[OrderType, float] | None:
        """Sortie déclenchée par la bougie, avec le prix de référence de l'exécution.

        Hypothèse conservatrice : si stop et objectif sont tous deux touchés dans la même
        bougie, le stop est considéré comme exécuté en premier. Un gap à l'ouverture au-delà
        d'un niveau est exécuté au prix d'ouverture.
        """
        stop_type = OrderType.TRAILING_STOP if self.trailing_active else OrderType.STOP_LOSS
        s = self.direction.sign
        if s * (candle.open - self.stop_loss) <= 0:
            return stop_type, candle.open
        if s * (candle.open - self.take_profit) >= 0:
            return OrderType.TAKE_PROFIT, candle.open
        adverse = candle.low if self.direction is Direction.LONG else candle.high
        favorable = candle.high if self.direction is Direction.LONG else candle.low
        if s * (adverse - self.stop_loss) <= 0:
            return stop_type, self.stop_loss
        if s * (favorable - self.take_profit) >= 0:
            return OrderType.TAKE_PROFIT, self.take_profit
        return None


@dataclass(frozen=True, slots=True)
class Trade:
    id: str
    seq: int
    position_id: str
    symbol: str
    direction: Direction
    side: Side
    order_type: OrderType
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    fees: float
    slippage: float
    gross_pnl: float
    net_pnl: float
    return_pct: float
    r_multiple: float
    strategy: str
    reason: str
    entry_reason: str = ""

    @property
    def timestamp(self) -> datetime:
        return self.exit_time
