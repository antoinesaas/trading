"""Positions ouvertes (avec sorties partielles) et trades clôturés."""

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
    initial_quantity: float = 0.0
    tp1_price: float | None = None
    tp1_fraction: float = 0.0
    tp1_done: bool = False
    breakeven_at_r: float = 0.0
    breakeven_done: bool = False
    time_stop_at: datetime | None = None
    stop_reason: str = "Stop-loss touché"
    realized_pnl: float = 0.0  # P&L brut déjà réalisé par les sorties partielles
    exit_fees: float = 0.0
    exit_slippage: float = 0.0
    exit_notional: float = 0.0
    best_price: float = 0.0  # meilleur prix atteint depuis l'entrée (excursion favorable)
    decision_id: int | None = None
    confidence: float | None = None
    fx_rate: float = 1.0  # devise de cotation -> devise du compte, figé à l'entrée

    def __post_init__(self) -> None:
        self.last_price = self.last_price or self.entry_price
        self.initial_quantity = self.initial_quantity or self.quantity
        self.best_price = self.best_price or self.entry_price

    @property
    def side(self) -> Side:
        return self.direction.entry_side

    @property
    def cost_basis(self) -> float:
        """Capital immobilisé, en devise du compte."""
        return self.entry_price * self.quantity * self.fx_rate

    @property
    def initial_risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_stop_loss)

    def unrealized_pnl(self, price: float | None = None) -> float:
        """P&L latent en devise du compte."""
        mark = self.last_price if price is None else price
        return self.direction.sign * (mark - self.entry_price) * self.quantity * self.fx_rate

    def favorable_move(self, price: float) -> float:
        return self.direction.sign * (price - self.entry_price)

    def r_multiple(self, price: float | None = None) -> float:
        risk = self.initial_risk_per_unit
        return self.favorable_move(self.last_price if price is None else price) / risk if risk else 0.0

    def track_extremes(self, candle: Candle) -> None:
        best = candle.high if self.direction is Direction.LONG else candle.low
        if self.direction.sign * (best - self.best_price) > 0:
            self.best_price = best

    def exit_events(self, candle: Candle) -> list[tuple[OrderType, float]]:
        """Sorties déclenchées par la bougie, dans l'ordre, avec leur prix de référence.

        Hypothèses prudentes : un gap à l'ouverture au-delà d'un niveau est exécuté à
        l'ouverture ; si le stop et un objectif sont touchés dans la même bougie, le stop
        est considéré comme exécuté en premier.
        """
        s = self.direction.sign
        stop_type = OrderType.TRAILING_STOP if self.trailing_active else OrderType.STOP_LOSS
        if s * (candle.open - self.stop_loss) <= 0:
            return [(stop_type, candle.open)]
        if s * (candle.open - self.take_profit) >= 0:
            return [(OrderType.TAKE_PROFIT, candle.open)]
        events: list[tuple[OrderType, float]] = []
        tp1_pending = self.tp1_price is not None and not self.tp1_done
        if tp1_pending and s * (candle.open - self.tp1_price) >= 0:  # type: ignore[operator]
            events.append((OrderType.TAKE_PROFIT_1, candle.open))
            tp1_pending = False
        adverse = candle.low if self.direction is Direction.LONG else candle.high
        favorable = candle.high if self.direction is Direction.LONG else candle.low
        if s * (adverse - self.stop_loss) <= 0:
            return events + [(stop_type, self.stop_loss)]
        if tp1_pending and s * (favorable - self.tp1_price) >= 0:  # type: ignore[operator]
            events.append((OrderType.TAKE_PROFIT_1, self.tp1_price))  # type: ignore[arg-type]
        if s * (favorable - self.take_profit) >= 0:
            events.append((OrderType.TAKE_PROFIT, self.take_profit))
        return events


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
    exit_price: float  # prix moyen de sortie (sorties partielles incluses)
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
    decision_id: int | None = None
    confidence: float | None = None
    max_favorable_r: float = 0.0

    @property
    def timestamp(self) -> datetime:
        return self.exit_time
