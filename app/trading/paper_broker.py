"""Broker simulé (paper trading). Aucune communication réseau.

Règles d'exécution (identiques en backtest et en paper trading temps réel) :
- MARKET : exécuté à l'ouverture de la bougie suivante, slippage défavorable appliqué ;
- LIMIT : exécuté si le prix atteint la limite (au prix d'ouverture s'il est meilleur),
  sans slippage ;
- STOP_LOSS / TRAILING_STOP : exécutés au niveau du stop (ou à l'ouverture en cas de
  gap), slippage défavorable appliqué ;
- TAKE_PROFIT : exécuté au niveau de l'objectif (ou à l'ouverture si gap favorable),
  sans slippage ;
- frais = notionnel exécuté x ``fee_rate``, à l'entrée et à la sortie.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import ClassVar

from app.core.events import EventBus, EventType
from app.core.types import (
    Candle, Direction, OrderIntent, OrderStatus, OrderType, Side, to_payload,
)
from app.risk.risk_manager import RiskLimits, round_down_to_step
from app.trading.broker import Broker
from app.trading.orders import IdGenerator, Order, OrderRequest, apply_slippage
from app.trading.portfolio import Portfolio
from app.trading.positions import Position

logger = logging.getLogger(__name__)

_SLIPPED_EXITS = frozenset({OrderType.MARKET, OrderType.STOP_LOSS, OrderType.TRAILING_STOP})


class OrderRejectedError(ValueError):
    """Ordre invalide refusé à la soumission."""


class PaperBroker(Broker):
    is_live: ClassVar[bool] = False

    def __init__(self, portfolio: Portfolio, bus: EventBus, limits: RiskLimits,
                 ids: IdGenerator | None = None, log: logging.Logger | None = None) -> None:
        self.log = log or logger
        self.portfolio = portfolio
        self.bus = bus
        self.limits = limits
        self.ids = ids or IdGenerator()
        self._pending: list[Order] = []

    # -- Soumission -------------------------------------------------------------
    def submit_order(self, request: OrderRequest, now: datetime) -> Order:
        self._validate(request)
        order_id, seq = self.ids.next("PO")
        order = Order(
            id=order_id, seq=seq, symbol=request.symbol, side=request.side,
            order_type=request.order_type, intent=request.intent, quantity=request.quantity,
            status=OrderStatus.PENDING, created_at=now, strategy=request.strategy,
            reason=request.reason, limit_price=request.limit_price,
            reference_price=request.reference_price, stop_distance=request.stop_distance,
            take_profit_distance=request.take_profit_distance,
        )
        self._pending.append(order)
        self.log.info("Paper Order #%d créé : %s %s %s qty=%g (%s)", seq, order.order_type, order.side,
                    order.symbol, order.quantity, order.reason)
        self._publish_order(order)
        return order

    def _validate(self, request: OrderRequest) -> None:
        if not (math.isfinite(request.quantity) and request.quantity > 0):
            raise OrderRejectedError("Quantité invalide")
        if request.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise OrderRejectedError("Seuls les ordres MARKET et LIMIT peuvent être soumis")
        if request.order_type is OrderType.LIMIT and not (request.limit_price or 0) > 0:
            raise OrderRejectedError("Un ordre LIMIT exige un prix limite > 0")
        if request.intent is OrderIntent.OPEN and not (request.stop_distance or 0) > 0:
            raise OrderRejectedError("Un ordre d'entrée exige une distance de stop > 0")

    def cancel_order(self, order_id: str, reason: str) -> bool:
        for order in self._pending:
            if order.id == order_id:
                self._pending.remove(order)
                order.status, order.reject_reason = OrderStatus.CANCELLED, reason
                self.log.info("Paper Order #%d annulé : %s", order.seq, reason)
                self._publish_order(order)
                return True
        return False

    def cancel_pending_entries(self, reason: str) -> None:
        for order in [o for o in self._pending if o.intent is OrderIntent.OPEN]:
            self.cancel_order(order.id, reason)

    def pending_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._pending if symbol is None or o.symbol == symbol]

    def close_position(self, symbol: str, reason: str, now: datetime) -> Order | None:
        position = self.portfolio.position(symbol)
        already_closing = any(o.intent is OrderIntent.CLOSE for o in self.pending_orders(symbol))
        if position is None or already_closing:
            return None
        return self.submit_order(OrderRequest(
            symbol=symbol, side=position.direction.exit_side, quantity=position.quantity,
            intent=OrderIntent.CLOSE, reference_price=position.last_price,
            strategy=position.strategy, reason=reason,
        ), now)

    def modify_stop(self, symbol: str, new_stop: float, now: datetime, reason: str) -> None:
        position = self.portfolio.position(symbol)
        if position is None:
            return
        position.stop_loss = new_stop
        position.trailing_active = True
        self.log.info("Trailing stop %s déplacé à %.8g (%s)", symbol, new_stop, reason)
        self.bus.publish(EventType.POSITION_UPDATED, to_payload(position))

    # -- Exécution ------------------------------------------------------------------
    def process_bar(self, candle: Candle) -> None:
        for order in [o for o in self._pending if o.symbol == candle.symbol]:
            self._try_fill(order, candle)
        self._check_exit(candle)
        self.portfolio.update_price(candle.symbol, candle.close)

    def _try_fill(self, order: Order, candle: Candle) -> None:
        # Exécution uniquement sur une bougie ouverte après l'ordre : jamais à un prix passé.
        if candle.open_time < order.created_at:
            return
        price = self._fill_price(order, candle)
        if price is None:
            return
        self._pending.remove(order)
        if order.intent is OrderIntent.OPEN:
            self._fill_entry(order, price, candle)
        else:
            self._fill_close(order, price, candle)

    def _fill_price(self, order: Order, candle: Candle) -> float | None:
        if order.order_type is OrderType.MARKET:
            return apply_slippage(candle.open, order.side, self.limits.slippage_rate)
        limit = order.limit_price or 0.0
        if order.side is Side.BUY:
            return candle.open if candle.open <= limit else (limit if candle.low <= limit else None)
        return candle.open if candle.open >= limit else (limit if candle.high >= limit else None)

    def _fill_entry(self, order: Order, price: float, candle: Candle) -> None:
        if self.portfolio.position(order.symbol) is not None:
            self._reject(order, "Position déjà ouverte sur ce symbole")
            return
        quantity = self._affordable_quantity(order.quantity, price)
        if quantity < self.limits.min_qty:
            self._reject(order, "Fonds insuffisants à l'exécution")
            return
        if quantity < order.quantity:
            self.log.warning("Paper Order #%d réduit de %g à %g (fonds disponibles)", order.seq,
                           order.quantity, quantity)
        direction = Direction.LONG if order.side is Side.BUY else Direction.SHORT
        stop = price - direction.sign * (order.stop_distance or 0.0)
        take_profit = price + direction.sign * (order.take_profit_distance or 0.0)
        fee = quantity * price * self.limits.fee_rate
        slippage = abs(price - candle.open) * quantity if order.order_type is OrderType.MARKET else 0.0
        position_id, position_seq = self.ids.next("POS")
        position = Position(
            id=position_id, seq=position_seq, symbol=order.symbol, direction=direction,
            quantity=quantity, entry_price=price, entry_time=candle.open_time, stop_loss=stop,
            take_profit=take_profit, initial_stop_loss=stop, entry_fee=fee, entry_slippage=slippage,
            strategy=order.strategy, entry_order_id=order.id, entry_reason=order.reason,
        )
        self.portfolio.open_position(position)
        self._mark_filled(order, price, candle.open_time, fee, slippage, position.id, quantity)
        order.stop_loss, order.take_profit = stop, take_profit
        self._publish_order(order)
        self.log.info("Position ouverte à %.8g (%s %s qty=%g)", price, direction, order.symbol, quantity)
        self.log.info("Stop Loss = %.8g", stop)
        self.log.info("Take Profit = %.8g", take_profit)
        self.bus.publish(EventType.POSITION_OPENED, to_payload(position))

    def _affordable_quantity(self, quantity: float, price: float) -> float:
        unit_cost = price * (1 + self.limits.fee_rate)
        affordable = round_down_to_step(max(self.portfolio.available_cash(), 0.0) / unit_cost,
                                        self.limits.qty_step)
        return min(quantity, affordable)

    def _fill_close(self, order: Order, price: float, candle: Candle) -> None:
        position = self.portfolio.position(order.symbol)
        if position is None:
            self._reject(order, "Aucune position à clôturer")
            return
        self._close(position, candle.open, candle.open_time, OrderType.MARKET, order.reason, order)

    def _check_exit(self, candle: Candle) -> None:
        position = self.portfolio.position(candle.symbol)
        if position is None:
            return
        trigger = position.exit_trigger(candle)
        if trigger is None:
            return
        order_type, level = trigger
        label = {OrderType.STOP_LOSS: "Stop-loss touché", OrderType.TRAILING_STOP: "Trailing stop touché",
                 OrderType.TAKE_PROFIT: "Take-profit atteint"}[order_type]
        # Horodatage = ouverture de la bougie d'exécution (identique en backtest et en temps réel).
        self._close(position, level, candle.open_time, order_type, label)

    def _close(self, position: Position, level: float, when: datetime, order_type: OrderType,
               reason: str, order: Order | None = None) -> None:
        side = position.direction.exit_side
        slipped = order_type in _SLIPPED_EXITS
        price = apply_slippage(level, side, self.limits.slippage_rate) if slipped else level
        fee = position.quantity * price * self.limits.fee_rate
        slippage = abs(price - level) * position.quantity
        if order is None:
            order_id, seq = self.ids.next("PO")
            order = Order(id=order_id, seq=seq, symbol=position.symbol, side=side, order_type=order_type,
                          intent=OrderIntent.CLOSE, quantity=position.quantity,
                          status=OrderStatus.PENDING, created_at=when, strategy=position.strategy,
                          reason=reason, reference_price=level)
        trade_id, trade_seq = self.ids.next("TR")
        trade = self.portfolio.close_position(
            position.symbol, trade_id=trade_id, trade_seq=trade_seq, exit_price=price, exit_time=when,
            exit_fee=fee, exit_slippage=slippage, order_type=order_type, reason=reason,
        )
        self._mark_filled(order, price, when, fee, slippage, position.id, position.quantity)
        self._publish_order(order)
        self.log.info("Position %s %s clôturée à %.8g — %s — P&L net %.2f", position.direction,
                    position.symbol, price, reason, trade.net_pnl)
        self.bus.publish(EventType.POSITION_CLOSED, to_payload(position))
        self.bus.publish(EventType.TRADE, to_payload(trade))

    def force_close(self, symbol: str, price: float, when: datetime, reason: str) -> None:
        """Clôture immédiate au prix donné (fin de backtest)."""
        position = self.portfolio.position(symbol)
        if position is not None:
            self._close(position, price, when, OrderType.MARKET, reason)

    # -- Utilitaires ------------------------------------------------------------------
    @staticmethod
    def _mark_filled(order: Order, price: float, when: datetime, fee: float, slippage: float,
                     position_id: str, quantity: float) -> None:
        order.status, order.fill_price, order.filled_at = OrderStatus.FILLED, price, when
        order.fees, order.slippage, order.position_id, order.quantity = fee, slippage, position_id, quantity

    def _reject(self, order: Order, reason: str) -> None:
        order.status, order.reject_reason = OrderStatus.REJECTED, reason
        self.log.warning("Paper Order #%d refusé : %s", order.seq, reason)
        self._publish_order(order)

    def _publish_order(self, order: Order) -> None:
        self.bus.publish(EventType.ORDER, to_payload(order))
