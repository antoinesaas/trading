"""Portefeuille simulé : balance, equity, P&L réalisé/latent, drawdown.

Conventions comptables :
- ``balance`` = capital initial + P&L net réalisé - frais d'entrée des positions ouvertes ;
- ``equity``  = balance + P&L latent (mark-to-market au dernier prix) ;
- pas de levier : chaque position (long ou short) immobilise son notionnel d'entrée.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.types import OrderType
from app.trading.positions import Position, Trade


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    timestamp: datetime
    balance: float
    equity: float
    unrealized_pnl: float
    drawdown: float
    peak_equity: float
    open_positions: int


class Portfolio:
    def __init__(self, initial_capital: float, balance: float | None = None,
                 peak_equity: float | None = None) -> None:
        if initial_capital <= 0:
            raise ValueError("Le capital initial doit être > 0")
        self.initial_capital = initial_capital
        self.balance = initial_capital if balance is None else balance
        self.peak_equity = max(peak_equity or initial_capital, self.balance)
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []

    # -- Lecture ------------------------------------------------------------------
    def position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def reserved_cash(self) -> float:
        return sum(p.cost_basis for p in self.positions.values())

    def available_cash(self) -> float:
        return self.balance - self.reserved_cash()

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl() for p in self.positions.values())

    def equity(self) -> float:
        return self.balance + self.unrealized_pnl()

    def realized_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    def drawdown(self) -> float:
        equity = self.equity()
        peak = max(self.peak_equity, equity)
        return (peak - equity) / peak if peak > 0 else 0.0

    # -- Mutations ------------------------------------------------------------------
    def update_price(self, symbol: str, price: float) -> None:
        position = self.positions.get(symbol)
        if position is not None:
            position.last_price = price

    def open_position(self, position: Position) -> None:
        if position.symbol in self.positions:
            raise ValueError(f"Une position est déjà ouverte sur {position.symbol}")
        self.balance -= position.entry_fee
        self.positions[position.symbol] = position

    def realize_partial(self, symbol: str, quantity: float, price: float, fee: float,
                        slippage: float) -> float:
        """Sortie partielle : encaisse le P&L de ``quantity`` unités ; retourne le P&L brut."""
        position = self.positions[symbol]
        gross = position.direction.sign * (price - position.entry_price) * quantity * position.fx_rate
        position.quantity -= quantity
        position.realized_pnl += gross
        position.exit_fees += fee
        position.exit_slippage += slippage
        position.exit_notional += quantity * price
        self.balance += gross - fee
        return gross

    def close_position(self, symbol: str, *, trade_id: str, trade_seq: int, exit_price: float,
                       exit_time: datetime, exit_fee: float, exit_slippage: float,
                       order_type: OrderType, reason: str) -> Trade:
        position = self.positions.pop(symbol)
        final_gross = position.unrealized_pnl(exit_price)
        gross = position.realized_pnl + final_gross
        fees = position.entry_fee + position.exit_fees + exit_fee
        net = gross - fees
        self.balance += final_gross - exit_fee
        quantity = position.initial_quantity
        average_exit = (position.exit_notional + exit_price * position.quantity) / quantity
        initial_risk = position.initial_risk_per_unit * quantity * position.fx_rate
        cost = position.entry_price * quantity * position.fx_rate
        trade = Trade(
            id=trade_id, seq=trade_seq, position_id=position.id, symbol=symbol,
            direction=position.direction, side=position.side, order_type=order_type,
            entry_time=position.entry_time, exit_time=exit_time, entry_price=position.entry_price,
            exit_price=average_exit, quantity=quantity, stop_loss=position.initial_stop_loss,
            take_profit=position.take_profit, fees=fees,
            slippage=position.entry_slippage + position.exit_slippage + exit_slippage,
            gross_pnl=gross, net_pnl=net, return_pct=net / cost if cost else 0.0,
            r_multiple=net / initial_risk if initial_risk else 0.0,
            strategy=position.strategy, reason=reason, entry_reason=position.entry_reason,
            decision_id=position.decision_id, confidence=position.confidence,
            max_favorable_r=position.r_multiple(position.best_price),
        )
        self.trades.append(trade)
        return trade

    def adjust_capital(self, new_initial: float) -> float:
        """Modifie le capital de base comme un dépôt / retrait ; retourne la variation."""
        if new_initial <= 0:
            raise ValueError("Le capital doit être positif")
        delta = new_initial - self.initial_capital
        if self.available_cash() + delta < 0:
            raise ValueError("Retrait impossible : ce capital est immobilisé par des positions ouvertes")
        self.initial_capital = new_initial
        self.balance += delta
        self.peak_equity += delta
        return delta

    def snapshot(self, timestamp: datetime) -> EquitySnapshot:
        equity = self.equity()
        self.peak_equity = max(self.peak_equity, equity)
        return EquitySnapshot(
            timestamp=timestamp, balance=self.balance, equity=equity,
            unrealized_pnl=self.unrealized_pnl(), drawdown=self.drawdown(),
            peak_equity=self.peak_equity, open_positions=len(self.positions),
        )
