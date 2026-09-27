"""Gestion du risque : taille de position, stop-loss, take-profit, limites de compte.

Taille de position = risque autorisé / risque réel par unité, où le risque réel par
unité inclut la distance au stop, les frais d'entrée et de sortie et le slippage
attendu à la sortie sur stop. Exemple (sans frais) : capital 10 000, risque 1 %
=> 100 ; stop à 2 du prix d'entrée => 100 / 2 = 50 unités.

Le ``RiskManager`` est le dernier mot sur toute entrée, qu'elle vienne des règles,
de TradingView ou de Claude : ses limites sont codées et ne peuvent pas être levées
par l'IA.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.types import Direction, Signal
from app.risk.money_management import OpenRisk, PerformanceSnapshot, risk_fraction

if TYPE_CHECKING:
    from app.trading.positions import Position
    from app.trading.specs import MarketSpecs

logger = logging.getLogger(__name__)


class ExitParams(BaseModel):
    """Paramètres de sortie, ajustables par l'optimiseur (dans ``search_space``).

    ``0`` désactive la prise de profit partielle, le point mort et le time-stop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_atr_multiplier: float = Field(2.0, gt=0, le=20)
    take_profit_risk_reward: float = Field(2.0, gt=0, le=20)
    trailing_stop_enabled: bool = False
    trailing_stop_atr_multiplier: float = Field(2.0, gt=0, le=20)
    trailing_activation_r: float = Field(1.0, ge=0, le=20)
    partial_take_profit_r: float = Field(0.0, ge=0, le=10)
    partial_take_profit_fraction: float = Field(0.5, gt=0, lt=1)
    breakeven_at_r: float = Field(0.0, ge=0, le=10)
    time_stop_bars: int = Field(0, ge=0, le=1_000)

    search_space: ClassVar[dict[str, tuple[float, float]]] = {
        "stop_loss_atr_multiplier": (1.0, 5.0),
        "take_profit_risk_reward": (1.0, 5.0),
        "trailing_stop_atr_multiplier": (1.0, 5.0),
        "trailing_activation_r": (0.0, 3.0),
        "partial_take_profit_r": (0.0, 3.0),
        "partial_take_profit_fraction": (0.2, 0.8),
        "breakeven_at_r": (0.0, 3.0),
        "time_stop_bars": (0, 96),
    }


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Limites de compte fixées par la configuration — jamais modifiées par l'IA."""

    risk_per_trade: float = 0.01
    hard_max_risk_per_trade: float = 0.02
    min_risk_per_trade: float = 0.0025
    max_open_positions: int = 3
    max_portfolio_risk: float = 0.05
    max_correlated_risk: float = 0.03
    max_daily_loss: float = 0.03
    max_weekly_loss: float = 0.06
    max_drawdown: float = 0.10
    max_consecutive_losses: int = 4
    cooldown_hours: float = 6.0
    min_reward_risk: float = 1.5
    min_stop_atr: float = 0.5
    max_stop_atr: float = 6.0
    min_confidence: float = 0.6
    kelly_multiplier: float = 0.25
    kelly_min_trades: int = 20
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
    open_risk: OpenRisk = field(default_factory=OpenRisk)
    stats: PerformanceSnapshot = field(default_factory=PerformanceSnapshot)
    drawdown: float = 0.0
    now: datetime | None = None


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
    risk_fraction: float = 0.0
    notes: tuple[str, ...] = ()


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


def compute_position_size(equity: float, risk_fraction_: float, entry_price: float, stop_loss: float,
                          qty_step: float, fee_rate: float = 0.0, slippage_rate: float = 0.0) -> float:
    unit_risk = risk_per_unit(entry_price, stop_loss, fee_rate, slippage_rate)
    if unit_risk <= 0:
        raise ValueError("Le risque par unité doit être > 0")
    return round_down_to_step(equity * risk_fraction_ / unit_risk, qty_step)


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


def breakeven_level(position: Position, close: float, fee_rate: float) -> float | None:
    """``fee_rate`` : frais par côté de l'instrument."""
    """Stop au point mort (frais aller-retour couverts) une fois ``breakeven_at_r`` atteint."""
    if position.breakeven_at_r <= 0 or position.breakeven_done:
        return None
    if position.favorable_move(close) < position.breakeven_at_r * position.initial_risk_per_unit:
        return None
    level = position.entry_price * (1 + position.direction.sign * 2 * fee_rate)
    improves = position.direction.sign * (level - position.stop_loss) > 0
    return level if improves else None


def open_risk(positions: object, equity: float) -> OpenRisk:
    """Perte encore possible si tous les stops étaient touchés (0 si stop au-delà de l'entrée)."""
    long_risk = short_risk = 0.0
    for p in positions:  # type: ignore[attr-defined]
        loss = max(0.0, p.direction.sign * (p.entry_price - p.stop_loss)) * p.quantity * p.fx_rate
        if p.direction is Direction.LONG:
            long_risk += loss
        else:
            short_risk += loss
    if equity <= 0:
        return OpenRisk()
    return OpenRisk((long_risk + short_risk) / equity, long_risk / equity, short_risk / equity)


# --- Risk Manager -----------------------------------------------------------------

class RiskManager:
    def __init__(self, limits: RiskLimits, exit_params: ExitParams, currency: str = "USDT",
                 log: logging.Logger | None = None, specs: MarketSpecs | None = None) -> None:
        self.log = log or logger
        self.limits = limits
        self.specs = specs
        self.exit_params = exit_params
        self.currency = currency
        self.halted = False
        self.halt_reason: str | None = None
        self._day: date | None = None
        self._day_start_equity = 0.0
        self._daily_blocked = False
        self._week: tuple[int, int] | None = None
        self._week_start_equity = 0.0
        self._weekly_blocked = False
        self._last_equity = 0.0

    # -- Décision d'entrée ------------------------------------------------------
    def evaluate(self, signal: Signal, account: AccountSnapshot, *, stop_loss: float | None = None,
                 take_profit: float | None = None, requested_risk: float | None = None,
                 confidence: float = 1.0) -> RiskDecision:
        """Valide et dimensionne une entrée.

        Sans ``stop_loss``/``take_profit`` (mode règles), ils sont calculés depuis l'ATR ;
        s'ils sont fournis (décision de Claude), ils sont vérifiés (côté, distance en ATR,
        ratio rendement/risque minimal).
        """
        blocked = self.entries_blocked_reason(account)
        if blocked:
            return self._reject(signal, blocked)
        if not (math.isfinite(signal.price) and signal.price > 0 and math.isfinite(signal.atr)
                and signal.atr > 0):
            return self._reject(signal, "Prix ou ATR invalide")
        if account.open_positions >= self.limits.max_open_positions:
            return self._reject(signal, f"Nombre maximal de positions atteint "
                                        f"({self.limits.max_open_positions})")
        try:
            stop, target = self._levels(signal, stop_loss, take_profit)
        except ValueError as exc:
            return self._reject(signal, str(exc))
        return self._size(signal, account, stop, target, requested_risk, confidence)

    def _levels(self, signal: Signal, stop: float | None, target: float | None) -> tuple[float, float]:
        exits, lim, s = self.exit_params, self.limits, signal.direction.sign
        stop = stop if stop is not None else compute_stop_loss(
            signal.direction, signal.price, signal.atr, exits.stop_loss_atr_multiplier)
        if not (math.isfinite(stop) and stop > 0) or s * (signal.price - stop) <= 0:
            raise ValueError(f"Stop-loss {stop} du mauvais côté du prix {signal.price}")
        distance_atr = abs(signal.price - stop) / signal.atr
        if not lim.min_stop_atr <= distance_atr <= lim.max_stop_atr:
            raise ValueError(f"Stop à {distance_atr:.2f} ATR hors de [{lim.min_stop_atr}, {lim.max_stop_atr}]")
        target = target if target is not None else compute_take_profit(
            signal.direction, signal.price, stop, exits.take_profit_risk_reward)
        if not (math.isfinite(target) and target > 0) or s * (target - signal.price) <= 0:
            raise ValueError(f"Take-profit {target} du mauvais côté du prix {signal.price}")
        reward_risk = abs(target - signal.price) / abs(signal.price - stop)
        if reward_risk + 1e-9 < lim.min_reward_risk:
            raise ValueError(f"Rendement/risque {reward_risk:.2f} < minimum {lim.min_reward_risk}")
        return stop, target

    def _size(self, signal: Signal, account: AccountSnapshot, stop: float, take_profit: float,
              requested_risk: float | None, confidence: float) -> RiskDecision:
        lim = self.limits
        fraction, notes = risk_fraction(
            requested_risk if requested_risk is not None else lim.risk_per_trade,
            confidence=confidence, min_confidence=lim.min_confidence, hard_max=lim.hard_max_risk_per_trade,
            min_risk=lim.min_risk_per_trade, stats=account.stats, drawdown=account.drawdown,
            max_drawdown=lim.max_drawdown, kelly_multiplier=lim.kelly_multiplier,
            kelly_min_trades=lim.kelly_min_trades,
        )
        heat_left = lim.max_portfolio_risk - account.open_risk.total
        correlated_left = lim.max_correlated_risk - account.open_risk.same_direction(signal.direction)
        costs, fx = self._costs(signal.symbol)
        if min(heat_left, correlated_left) < fraction:
            fraction = min(heat_left, correlated_left)
            notes.append(f"limité par le risque ouvert (total {account.open_risk.total:.2%}, "
                         f"même sens {account.open_risk.same_direction(signal.direction):.2%})")
        if fraction < lim.min_risk_per_trade / 2:
            return self._reject(signal, "Budget de risque épuisé (risque total ou corrélé au plafond)")
        self.log.info("Risk Manager: risque autorisé = %.2f %s (%.2f%%)%s", account.equity * fraction,
                      self.currency, fraction * 100, f" — {'; '.join(notes)}" if notes else "")
        # Montants du compte = montants en devise de cotation x fx (ex. EUR -> USD).
        unit_risk = risk_per_unit(signal.price, stop, costs.fee_rate, costs.slippage_rate) * fx
        quantity = round_down_to_step(account.equity * fraction / unit_risk, costs.qty_step)
        unit_cost = signal.price * (1 + costs.fee_rate + costs.slippage_rate) * fx
        cash_cap = round_down_to_step(max(account.available_cash, 0.0) / unit_cost, costs.qty_step)
        exposure_cap = round_down_to_step(account.equity * lim.max_position_pct / (signal.price * fx),
                                          costs.qty_step)
        quantity = min(quantity, cash_cap, exposure_cap)
        if quantity < costs.min_qty:
            return self._reject(signal, f"Taille {quantity:g} inférieure au minimum {costs.min_qty:g}")
        if quantity * signal.price < costs.min_notional:
            return self._reject(signal, f"Notionnel {quantity * signal.price:.2f} < minimum "
                                        f"{costs.min_notional:.2f}")
        return RiskDecision(
            approved=True, reason="Risque accepté", quantity=quantity, entry_price=signal.price,
            stop_loss=stop, take_profit=take_profit, stop_distance=abs(signal.price - stop),
            take_profit_distance=abs(take_profit - signal.price), risk_amount=quantity * unit_risk,
            risk_fraction=fraction, notes=tuple(notes),
        )

    def _costs(self, symbol: str) -> tuple[object, float]:
        if self.specs is not None:
            return self.specs.costs(symbol), self.specs.fx_rate(symbol)
        from app.trading.specs import MarketSpecs  # import local : évite un cycle
        return MarketSpecs(self.limits).costs(symbol), 1.0

    def _reject(self, signal: Signal, reason: str) -> RiskDecision:
        self.log.warning("Risk Manager: signal %s %s refusé — %s", signal.direction, signal.symbol, reason)
        return RiskDecision(approved=False, reason=reason)

    # -- Limites de compte ------------------------------------------------------
    def entries_blocked_reason(self, account: AccountSnapshot | None = None) -> str | None:
        if self.halted:
            return f"Kill-switch actif : {self.halt_reason}"
        if self._daily_blocked:
            return f"Perte journalière maximale atteinte ({self.limits.max_daily_loss:.1%})"
        if self._weekly_blocked:
            return f"Perte hebdomadaire maximale atteinte ({self.limits.max_weekly_loss:.1%})"
        stats = account.stats if account is not None else None
        if (stats is not None and stats.consecutive_losses >= self.limits.max_consecutive_losses
                and stats.last_loss_time is not None and account is not None and account.now is not None):
            resume = stats.last_loss_time + timedelta(hours=self.limits.cooldown_hours)
            if account.now < resume:
                return (f"Pause après {stats.consecutive_losses} pertes consécutives "
                        f"jusqu'à {resume:%Y-%m-%d %H:%M} UTC")
        return None

    def update_equity(self, timestamp: datetime, equity: float, peak_equity: float) -> list[RiskAlert]:
        """Met à jour les limites ; retourne les alertes nouvellement déclenchées."""
        self._roll_periods(timestamp, equity)
        self._last_equity = equity
        alerts: list[RiskAlert] = []
        if not self._daily_blocked and self.daily_loss(equity) >= self.limits.max_daily_loss:
            self._daily_blocked = True
            message = (f"Perte journalière {self.daily_loss(equity):.2%} >= "
                       f"{self.limits.max_daily_loss:.2%} : nouvelles entrées bloquées jusqu'à demain")
            self.log.warning(message)
            alerts.append(RiskAlert("daily_loss", message))
        if not self._weekly_blocked and self.weekly_loss(equity) >= self.limits.max_weekly_loss:
            self._weekly_blocked = True
            message = (f"Perte hebdomadaire {self.weekly_loss(equity):.2%} >= "
                       f"{self.limits.max_weekly_loss:.2%} : nouvelles entrées bloquées jusqu'à lundi")
            self.log.warning(message)
            alerts.append(RiskAlert("weekly_loss", message))
        drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
        if not self.halted and drawdown >= self.limits.max_drawdown:
            self.halted = True
            self.halt_reason = f"drawdown {drawdown:.2%} >= {self.limits.max_drawdown:.2%}"
            message = f"KILL-SWITCH : {self.halt_reason}. Trading arrêté."
            self.log.critical(message)
            alerts.append(RiskAlert("max_drawdown", message))
        return alerts

    def _roll_periods(self, timestamp: datetime, equity: float) -> None:
        if self._day != timestamp.date():
            self._day = timestamp.date()
            self._day_start_equity = equity
            self._daily_blocked = False
        week = timestamp.isocalendar()[:2]
        if self._week != week:
            self._week = (week[0], week[1])
            self._week_start_equity = equity
            self._weekly_blocked = False

    @staticmethod
    def _loss(start: float, current: float) -> float:
        return max(0.0, (start - current) / start) if start > 0 else 0.0

    def daily_loss(self, equity: float | None = None) -> float:
        return self._loss(self._day_start_equity, self._last_equity if equity is None else equity)

    def weekly_loss(self, equity: float | None = None) -> float:
        return self._loss(self._week_start_equity, self._last_equity if equity is None else equity)

    def reset_halt(self) -> None:
        self.log.warning("Kill-switch réinitialisé manuellement.")
        self.halted = False
        self.halt_reason = None

    def restore(self, day: date | None, day_start_equity: float, halted: bool,
                halt_reason: str | None, week_start_equity: float = 0.0) -> None:
        self._day = day
        self._day_start_equity = day_start_equity
        self._last_equity = self._last_equity or day_start_equity
        if day is not None and week_start_equity:
            self._week = (day.isocalendar()[0], day.isocalendar()[1])
            self._week_start_equity = week_start_equity
        self.halted = halted
        self.halt_reason = halt_reason

    def shift_baselines(self, delta: float) -> None:
        """Après un dépôt / retrait, décale les références de perte journalière et hebdomadaire."""
        if self._day_start_equity:
            self._day_start_equity += delta
        if self._week_start_equity:
            self._week_start_equity += delta

    def set_exit_params(self, exit_params: ExitParams) -> None:
        self.exit_params = exit_params

    def status(self, equity: float | None = None) -> dict[str, object]:
        """État des limites ; ``equity`` = equity courante (sinon la dernière reçue)."""
        current = equity if equity is not None else self._last_equity or None
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "daily_loss": self.daily_loss(current) if current else 0.0,
            "weekly_loss": self.weekly_loss(current) if current else 0.0,
            "daily_limit_reached": self._daily_blocked,
            "weekly_limit_reached": self._weekly_blocked,
            "day_start_equity": self._day_start_equity,
            "entries_blocked": self.entries_blocked_reason(),
        }
