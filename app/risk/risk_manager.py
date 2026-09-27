"""Gestion du risque : taille de position, stop-loss, take-profit, limites de compte.

Taille de position = risque autorisé / risque réel par unité, où le risque réel par
unité inclut la distance au stop, les frais d'entrée et de sortie et le slippage
attendu à la sortie sur stop. Exemple (sans frais) : capital 10 000, risque 1 %
=> 100 ; stop à 2 du prix d'entrée => 100 / 2 = 50 unités.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.types import Direction, Signal

if TYPE_CHECKING:
    from app.trading.positions import Position

logger = logging.getLogger(__name__)


class ExitParams(BaseModel):
    """Paramètres de sortie, ajustables par l'optimiseur (dans ``search_space``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_atr_multiplier: float = Field(2.0, gt=0, le=20)
    take_profit_risk_reward: float = Field(2.0, gt=0, le=20)
    trailing_stop_enabled: bool = False
    trailing_stop_atr_multiplier: float = Field(2.0, gt=0, le=20)
    trailing_activation_r: float = Field(1.0, ge=0, le=20)

    search_space: ClassVar[dict[str, tuple[float, float]]] = {
        "stop_loss_atr_multiplier": (1.0, 5.0),
        "take_profit_risk_reward": (1.0, 5.0),
        "trailing_stop_atr_multiplier": (1.0, 5.0),
        "trailing_activation_r": (0.0, 3.0),
    }


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Limites de compte fixées par la configuration — jamais modifiées par l'IA."""

    risk_per_trade: float = 0.01
    max_open_positions: int = 3
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    max_position_pct: float = 1.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    min_qty: float = 0.0001
    qty_step: float = 0.0001
    min_notional: float = 10.0
    close_positions_on_max_drawdown: bool = True


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity: float
    available_cash: float
    open_positions: int


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str
    quantity: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    stop_distance: float = 0.0
    take_profit_distance: float = 0.0
    risk_amount: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskAlert:
    kind: str
    message: str


# --- Calculs purs -----------------------------------------------------------------

def round_down_to_step(quantity: float, step: float) -> float:
    """Arrondit vers le bas au multiple de ``step`` (tolère le bruit flottant)."""
    if step <= 0:
        raise ValueError("step doit être > 0")
    if quantity <= 0 or not math.isfinite(quantity):
        return 0.0
    q, s = Decimal(repr(quantity)), Decimal(repr(step))
    units = q / s
    nearest = units.to_integral_value()
    if abs(units - nearest) < Decimal("1e-9"):
        units = nearest
    return float(units.to_integral_value(rounding=ROUND_FLOOR) * s)


def compute_stop_loss(direction: Direction, entry_price: float, atr: float, multiplier: float) -> float:
    distance = atr * multiplier
    if entry_price <= 0 or distance <= 0 or not math.isfinite(distance):
        raise ValueError("Prix d'entrée et distance de stop doivent être > 0")
    stop = entry_price - direction.sign * distance
    if stop <= 0:
        raise ValueError(f"Stop-loss calculé invalide ({stop:.8f})")
    return stop


def compute_take_profit(direction: Direction, entry_price: float, stop_loss: float,
                        risk_reward: float) -> float:
    if risk_reward <= 0:
        raise ValueError("Le ratio risque/rendement doit être > 0")
    take_profit = entry_price + direction.sign * abs(entry_price - stop_loss) * risk_reward
    if take_profit <= 0:
        raise ValueError(f"Take-profit calculé invalide ({take_profit:.8f})")
    return take_profit


def risk_per_unit(entry_price: float, stop_loss: float, fee_rate: float = 0.0,
                  slippage_rate: float = 0.0) -> float:
    """Perte réelle par unité si le stop est touché (distance + frais + slippage de sortie)."""
    return abs(entry_price - stop_loss) + entry_price * fee_rate + stop_loss * (fee_rate + slippage_rate)


def compute_position_size(equity: float, risk_fraction: float, entry_price: float, stop_loss: float,
                          qty_step: float, fee_rate: float = 0.0, slippage_rate: float = 0.0) -> float:
    unit_risk = risk_per_unit(entry_price, stop_loss, fee_rate, slippage_rate)
    if unit_risk <= 0:
        raise ValueError("Le risque par unité doit être > 0")
    return round_down_to_step(equity * risk_fraction / unit_risk, qty_step)


def trailing_stop_level(position: Position, close: float, atr: float,
                        exit_params: ExitParams) -> float | None:
    """Nouveau stop suiveur, ou ``None`` s'il n'est pas activé ou n'améliore pas le stop.

    Le trailing s'active quand le gain latent atteint ``trailing_activation_r`` fois le
    risque initial ; le stop ne se déplace que dans le sens favorable.
    """
    activation = exit_params.trailing_activation_r * position.initial_risk_per_unit
    if position.favorable_move(close) < activation:
        return None
    candidate = close - position.direction.sign * atr * exit_params.trailing_stop_atr_multiplier
    improves = position.direction.sign * (candidate - position.stop_loss) > 0
    return candidate if improves and candidate > 0 else None


# --- Risk Manager -----------------------------------------------------------------

class RiskManager:
    def __init__(self, limits: RiskLimits, exit_params: ExitParams, currency: str = "USDT",
                 log: logging.Logger | None = None) -> None:
        self.log = log or logger
        self.limits = limits
        self.exit_params = exit_params
        self.currency = currency
        self.halted = False
        self.halt_reason: str | None = None
        self._day: date | None = None
        self._day_start_equity = 0.0
        self._daily_blocked = False
        self._last_equity = 0.0

    # -- Décision d'entrée ------------------------------------------------------
    def evaluate(self, signal: Signal, account: AccountSnapshot) -> RiskDecision:
        blocked = self.entries_blocked_reason()
        if blocked:
            return self._reject(signal, blocked)
        if not (math.isfinite(signal.price) and signal.price > 0 and math.isfinite(signal.atr)
                and signal.atr > 0):
            return self._reject(signal, "Prix ou ATR invalide")
        if account.open_positions >= self.limits.max_open_positions:
            return self._reject(signal, f"Nombre maximal de positions atteint "
                                        f"({self.limits.max_open_positions})")
        try:
            stop = compute_stop_loss(signal.direction, signal.price, signal.atr,
                                     self.exit_params.stop_loss_atr_multiplier)
            take_profit = compute_take_profit(signal.direction, signal.price, stop,
                                              self.exit_params.take_profit_risk_reward)
        except ValueError as exc:
            return self._reject(signal, str(exc))
        return self._size(signal, account, stop, take_profit)

    def _size(self, signal: Signal, account: AccountSnapshot, stop: float,
              take_profit: float) -> RiskDecision:
        lim = self.limits
        risk_budget = account.equity * lim.risk_per_trade
        self.log.info("Risk Manager: risque autorisé = %.2f %s", risk_budget, self.currency)
        quantity = compute_position_size(account.equity, lim.risk_per_trade, signal.price, stop,
                                         lim.qty_step, lim.fee_rate, lim.slippage_rate)
        unit_cost = signal.price * (1 + lim.fee_rate + lim.slippage_rate)
        cash_cap = round_down_to_step(max(account.available_cash, 0.0) / unit_cost, lim.qty_step)
        exposure_cap = round_down_to_step(account.equity * lim.max_position_pct / signal.price,
                                          lim.qty_step)
        quantity = min(quantity, cash_cap, exposure_cap)
        if quantity < lim.min_qty:
            return self._reject(signal, f"Taille {quantity:g} inférieure au minimum {lim.min_qty:g}")
        if quantity * signal.price < lim.min_notional:
            return self._reject(signal, f"Notionnel {quantity * signal.price:.2f} < minimum "
                                        f"{lim.min_notional:.2f}")
        unit_risk = risk_per_unit(signal.price, stop, lim.fee_rate, lim.slippage_rate)
        return RiskDecision(
            approved=True, reason="Risque accepté", quantity=quantity, entry_price=signal.price,
            stop_loss=stop, take_profit=take_profit, stop_distance=abs(signal.price - stop),
            take_profit_distance=abs(take_profit - signal.price), risk_amount=quantity * unit_risk,
        )

    def _reject(self, signal: Signal, reason: str) -> RiskDecision:
        self.log.warning("Risk Manager: signal %s %s refusé — %s", signal.direction, signal.symbol, reason)
        return RiskDecision(approved=False, reason=reason)

    # -- Limites de compte ------------------------------------------------------
    def entries_blocked_reason(self) -> str | None:
        if self.halted:
            return f"Kill-switch actif : {self.halt_reason}"
        if self._daily_blocked:
            return f"Perte journalière maximale atteinte ({self.limits.max_daily_loss:.1%})"
        return None

    def update_equity(self, timestamp: datetime, equity: float, peak_equity: float) -> list[RiskAlert]:
        """Met à jour les limites ; retourne les alertes nouvellement déclenchées."""
        self._roll_day(timestamp.date(), equity)
        self._last_equity = equity
        alerts: list[RiskAlert] = []
        if not self._daily_blocked and self.daily_loss(equity) >= self.limits.max_daily_loss:
            self._daily_blocked = True
            message = (f"Perte journalière {self.daily_loss(equity):.2%} >= "
                       f"{self.limits.max_daily_loss:.2%} : nouvelles entrées bloquées jusqu'à demain")
            self.log.warning(message)
            alerts.append(RiskAlert("daily_loss", message))
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
        if not self.halted and drawdown >= self.limits.max_drawdown:
            self.halted = True
            self.halt_reason = f"drawdown {drawdown:.2%} >= {self.limits.max_drawdown:.2%}"
            message = f"KILL-SWITCH : {self.halt_reason}. Trading arrêté."
            self.log.critical(message)
            alerts.append(RiskAlert("max_drawdown", message))
        return alerts

    def _roll_day(self, day: date, equity: float) -> None:
        if self._day != day:
            self._day = day
            self._day_start_equity = equity
            self._daily_blocked = False

    def daily_loss(self, equity: float | None = None) -> float:
        current = self._last_equity if equity is None else equity
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, (self._day_start_equity - current) / self._day_start_equity)

    def reset_halt(self) -> None:
        self.log.warning("Kill-switch réinitialisé manuellement.")
        self.halted = False
        self.halt_reason = None

    def restore(self, day: date | None, day_start_equity: float, halted: bool,
                halt_reason: str | None) -> None:
        self._day = day
        self._day_start_equity = day_start_equity
        self.halted = halted
        self.halt_reason = halt_reason

    def set_exit_params(self, exit_params: ExitParams) -> None:
        self.exit_params = exit_params

    def status(self) -> dict[str, object]:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "daily_loss": self.daily_loss(),
            "daily_limit_reached": self._daily_blocked,
            "day_start_equity": self._day_start_equity,
            "entries_blocked": self.entries_blocked_reason(),
        }
