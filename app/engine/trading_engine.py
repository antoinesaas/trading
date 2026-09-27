"""Moteur de trading : Market Data -> Stratégie -> Risk Manager -> Broker -> Portefeuille.

Le MÊME moteur est utilisé par le backtest et par le paper trading temps réel ;
seule la source des bougies change. Séquence par bougie :

1. ``on_bar_update(candle)`` : exécution des ordres en attente (à l'ouverture) puis
   sorties (stop, prise de profit partielle, objectif) sur le haut/bas de la bougie ;
2. ``on_bar_close(candle)``  : bougie clôturée -> gestion des positions (trailing, point
   mort, time-stop), equity, limites de risque, puis analyse de la stratégie.

En mode règles (``auto_execute=True``) un signal part directement au Risk Manager.
En mode IA (``auto_execute=False``) il est mis en file d'attente : c'est Claude qui
décide (``open_trade`` / ``manage_position``), toujours sous le contrôle du Risk Manager.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.events import EventBus, EventType
from app.core.types import Candle, OrderIntent, OrderType, Signal, to_payload
from app.engine.params import ParameterSet
from app.risk.money_management import PerformanceSnapshot
from app.risk.risk_manager import (
    AccountSnapshot, RiskManager, breakeven_level, open_risk, trailing_stop_level,
)
from app.strategy import Analysis, CandleWindow, Strategy
from app.trading.orders import OrderRequest
from app.trading.paper_broker import PaperBroker
from app.trading.portfolio import EquitySnapshot, Portfolio

logger = logging.getLogger(__name__)

TIME_STOP_MIN_R = 0.5  # un time-stop ne coupe que les positions qui n'ont pas progressé


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    signal: Signal
    status: str  # accepted | rejected | ignored | queued
    reason: str
    order_id: str | None = None


@dataclass(frozen=True, slots=True)
class TradePlan:
    """Plan d'exécution d'une entrée (décision de Claude ou paramètres de sortie par défaut)."""

    stop_loss: float | None = None
    take_profit: float | None = None
    requested_risk: float | None = None
    confidence: float = 1.0
    tp1_price: float | None = None
    tp1_fraction: float = 0.0
    breakeven_at_r: float = 0.0
    time_stop: timedelta | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    decision_id: int | None = None


class TradingEngine:
    def __init__(self, strategy: Strategy, risk: RiskManager, broker: PaperBroker,
                 portfolio: Portfolio, bus: EventBus, *, lookback_bars: int = 300,
                 internal_signals: bool = True, auto_execute: bool = True,
                 log: logging.Logger | None = None) -> None:
        self.log = log or logger
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.portfolio = portfolio
        self.bus = bus
        self.lookback_bars = lookback_bars
        self.internal_signals = internal_signals
        self.auto_execute = auto_execute
        self.entries_enabled = True
        self._windows: dict[str, deque[Candle]] = {}
        self._last_price: dict[str, float] = {}
        self._last_analysis: dict[str, Analysis] = {}
        self._candidates: list[Signal] = []

    # -- Paramètres -----------------------------------------------------------------
    @property
    def window_size(self) -> int:
        return max(self.lookback_bars, self.strategy.warmup_bars + 2)

    def current_params(self) -> ParameterSet:
        return ParameterSet(strategy_name=self.strategy.name, strategy=self.strategy.params.model_dump(),
                            exits=self.risk.exit_params)

    def apply_params(self, params: ParameterSet) -> None:
        """Change stratégie et sorties à chaud (les positions ouvertes gardent leurs niveaux)."""
        self.strategy = params.build_strategy()
        self.risk.set_exit_params(params.exits)
        for symbol, window in self._windows.items():
            self._windows[symbol] = deque(window, maxlen=self.window_size)
        self.log.info("Nouveaux paramètres appliqués : %s", params.model_dump())

    # -- Lecture ------------------------------------------------------------------------
    def window(self, symbol: str) -> list[Candle]:
        return list(self._windows.get(symbol, ()))

    def last_closed_time(self, symbol: str) -> datetime | None:
        window = self._windows.get(symbol)
        return window[-1].open_time if window else None

    def last_price(self, symbol: str) -> float | None:
        return self._last_price.get(symbol)

    def last_analysis(self, symbol: str) -> Analysis | None:
        return self._last_analysis.get(symbol)

    def drain_candidates(self) -> list[Signal]:
        candidates, self._candidates = self._candidates, []
        return candidates

    def account(self, now: datetime | None = None, exclude: str | None = None) -> AccountSnapshot:
        positions = [p for s, p in self.portfolio.positions.items() if s != exclude]
        pending = [o for o in self.broker.pending_orders() if o.intent is OrderIntent.OPEN]
        reserved = (sum(p.cost_basis for p in positions)
                    + sum(o.quantity * (o.reference_price or 0.0) for o in pending))
        equity = self.portfolio.equity()
        return AccountSnapshot(
            equity=equity, available_cash=self.portfolio.balance - reserved,
            open_positions=len(positions) + len(pending), open_risk=open_risk(positions, equity),
            stats=PerformanceSnapshot.from_trades(self.portfolio.trades),
            drawdown=self.portfolio.drawdown(), now=now,
        )

    # -- Flux de bougies ----------------------------------------------------------------
    def warm_up(self, candles: Sequence[Candle]) -> None:
        """Charge l'historique pour les indicateurs, sans trader."""
        for candle in candles:
            if candle.closed and self._append(candle):
                self._last_price[candle.symbol] = candle.close
                self.portfolio.update_price(candle.symbol, candle.close)

    def on_bar_update(self, candle: Candle) -> None:
        self._last_price[candle.symbol] = candle.close
        self.broker.process_bar(candle)

    def on_bar_close(self, candle: Candle, allow_entries: bool = True) -> SignalOutcome | None:
        if not self._append(candle):
            return None
        analysis = self.strategy.analyze(CandleWindow.from_candles(self._windows[candle.symbol]))
        self._last_analysis[candle.symbol] = analysis
        self._manage_positions(candle, analysis.atr)
        self._record_equity(candle.close_time)
        if analysis.signal is None or not allow_entries or not self.internal_signals:
            return None
        if self.auto_execute:
            return self.handle_signal(analysis.signal, candle.close_time)
        self._candidates.append(analysis.signal)
        return SignalOutcome(analysis.signal, "queued", "Transmis à l'IA pour décision")

    def _append(self, candle: Candle) -> bool:
        window = self._windows.setdefault(candle.symbol, deque(maxlen=self.window_size))
        if window and candle.open_time <= window[-1].open_time:
            return False
        window.append(candle)
        return True

    # -- Entrées -----------------------------------------------------------------------------
    def handle_signal(self, signal: Signal, now: datetime | None = None) -> SignalOutcome:
        """Mode règles : entrée avec les paramètres de sortie par défaut."""
        return self.open_trade(signal, self.default_plan(signal), now)

    def default_plan(self, signal: Signal) -> TradePlan:
        exits = self.risk.exit_params
        stop_distance = signal.atr * exits.stop_loss_atr_multiplier
        tp1 = (signal.price + signal.direction.sign * stop_distance * exits.partial_take_profit_r
               if exits.partial_take_profit_r > 0 else None)
        window = self._windows.get(signal.symbol)
        bar = (window[-1].close_time - window[-1].open_time) if window else timedelta(hours=1)
        return TradePlan(tp1_price=tp1, tp1_fraction=exits.partial_take_profit_fraction if tp1 else 0.0,
                         breakeven_at_r=exits.breakeven_at_r,
                         time_stop=bar * exits.time_stop_bars if exits.time_stop_bars else None)

    def open_trade(self, signal: Signal, plan: TradePlan, now: datetime | None = None) -> SignalOutcome:
        now = now or signal.timestamp
        self.log.info("Signal %s détecté sur %s (%s) — %s", signal.direction, signal.symbol,
                      signal.source, signal.reason)
        outcome = self._decide(signal, plan, now)
        payload = to_payload(signal) | {"status": outcome.status, "status_reason": outcome.reason,
                                        "order_id": outcome.order_id}
        self.bus.publish(EventType.SIGNAL, payload)
        return outcome

    def _decide(self, signal: Signal, plan: TradePlan, now: datetime) -> SignalOutcome:
        if not self.entries_enabled:
            return SignalOutcome(signal, "ignored", "Bot en pause ou arrêté : nouvelles entrées désactivées")
        position = self.portfolio.position(signal.symbol)
        if position is not None and position.direction is signal.direction:
            return SignalOutcome(signal, "ignored", "Position déjà ouverte dans ce sens")
        if any(o.intent is OrderIntent.OPEN for o in self.broker.pending_orders(signal.symbol)):
            return SignalOutcome(signal, "ignored", "Un ordre d'entrée est déjà en attente")
        reversing = position is not None
        if reversing:
            self.broker.close_position(signal.symbol, "Signal opposé", now)
        decision = self.risk.evaluate(
            signal, self.account(now, exclude=signal.symbol if reversing else None),
            stop_loss=plan.stop_loss, take_profit=plan.take_profit,
            requested_risk=plan.requested_risk, confidence=plan.confidence)
        if not decision.approved:
            return SignalOutcome(signal, "rejected", decision.reason)
        if plan.tp1_price is not None and not (
                0 < signal.direction.sign * (plan.tp1_price - signal.price) < decision.take_profit_distance):
            return SignalOutcome(signal, "rejected", "Objectif partiel (TP1) hors de la zone entrée-objectif")
        order = self.broker.submit_order(OrderRequest(
            symbol=signal.symbol, side=signal.direction.entry_side, quantity=decision.quantity,
            order_type=plan.order_type, limit_price=plan.limit_price,
            reference_price=plan.limit_price or signal.price, stop_distance=decision.stop_distance,
            take_profit_distance=decision.take_profit_distance,
            tp1_distance=abs(plan.tp1_price - signal.price) if plan.tp1_price is not None else None,
            tp1_fraction=plan.tp1_fraction, breakeven_at_r=plan.breakeven_at_r, time_stop=plan.time_stop,
            decision_id=plan.decision_id, confidence=plan.confidence if plan.decision_id else None,
            strategy=signal.strategy, reason=signal.reason,
        ), now)
        return SignalOutcome(signal, "accepted", f"Ordre créé (risque {decision.risk_fraction:.2%})", order.id)

    # -- Gestion des positions ------------------------------------------------------------------
    def manage_position(self, symbol: str, now: datetime, *, new_stop: float | None = None,
                        new_target: float | None = None, close: bool = False, reason: str = "") -> str:
        """Ajustement demandé par l'IA. Le stop ne peut que se resserrer (jamais s'élargir)."""
        position = self.portfolio.position(symbol)
        if position is None:
            return "Aucune position"
        if close:
            self.broker.close_position(symbol, reason or "Clôture décidée par l'IA", now)
            return "Clôture demandée"
        actions = []
        if new_stop is not None:
            s = position.direction.sign
            if s * (new_stop - position.stop_loss) <= 0:
                actions.append("stop refusé (élargissement interdit)")
            elif s * (position.last_price - new_stop) <= 0:
                actions.append("stop refusé (au-delà du prix actuel)")
            else:
                self.broker.modify_stop(symbol, new_stop, now, reason, kind="manual")
                actions.append(f"stop -> {new_stop:.8g}")
        if new_target is not None:
            if position.direction.sign * (new_target - position.last_price) <= 0:
                actions.append("objectif refusé (déjà dépassé)")
            else:
                self.broker.modify_take_profit(symbol, new_target, now, reason)
                actions.append(f"objectif -> {new_target:.8g}")
        return ", ".join(actions) or "Aucun changement"

    def _manage_positions(self, candle: Candle, atr: float | None) -> None:
        position = self.portfolio.position(candle.symbol)
        if position is None:
            return
        level = breakeven_level(position, candle.close, self.risk.limits.fee_rate)
        if level is not None:
            self.broker.modify_stop(candle.symbol, level, candle.close_time,
                                    f"+{position.breakeven_at_r:g}R atteint", kind="breakeven")
        exits = self.risk.exit_params
        if exits.trailing_stop_enabled and atr is not None:
            new_stop = trailing_stop_level(position, candle.close, atr, exits)
            if new_stop is not None:
                self.broker.modify_stop(candle.symbol, new_stop, candle.close_time,
                                        f"clôture {candle.close:.8g}, ATR {atr:.8g}")
        if position.time_stop_at is not None and candle.close_time >= position.time_stop_at:
            position.time_stop_at = None
            if position.r_multiple(candle.close) < TIME_STOP_MIN_R:
                self.broker.close_position(candle.symbol, "Time-stop : la position n'a pas progressé",
                                           candle.close_time)

    # -- Risque du compte ---------------------------------------------------------------------------
    def _record_equity(self, timestamp: datetime) -> EquitySnapshot:
        snapshot = self.portfolio.snapshot(timestamp)
        self.bus.publish(EventType.EQUITY, to_payload(snapshot))
        for alert in self.risk.update_equity(timestamp, snapshot.equity, snapshot.peak_equity):
            self.bus.publish(EventType.RISK, {"kind": alert.kind, "message": alert.message})
            if alert.kind == "max_drawdown":
                self._kill_switch(timestamp)
        return snapshot

    def _kill_switch(self, timestamp: datetime) -> None:
        self.broker.cancel_pending_entries("Kill-switch drawdown maximal")
        if self.risk.limits.close_positions_on_max_drawdown:
            for symbol in list(self.portfolio.positions):
                self.broker.close_position(symbol, "Kill-switch drawdown maximal", timestamp)
