"""Moteur de trading : Market Data -> Stratégie -> Risk Manager -> Broker -> Portefeuille.

Le MÊME moteur est utilisé par le backtest et par le paper trading temps réel ;
seule la source des bougies change. Séquence par bougie :

1. ``on_bar_update(candle)`` : exécution des ordres en attente (à l'ouverture) puis
   contrôle stop-loss / take-profit sur le haut/bas de la bougie ;
2. ``on_bar_close(candle)``  : bougie clôturée -> trailing stop, equity, limites de
   risque, puis analyse de la stratégie et éventuel nouvel ordre (exécuté à
   l'ouverture de la bougie suivante).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.core.events import EventBus, EventType
from app.core.types import Candle, OrderIntent, Signal, to_payload
from app.engine.params import ParameterSet
from app.risk.risk_manager import AccountSnapshot, RiskManager, trailing_stop_level
from app.strategy import Analysis, CandleWindow, Strategy
from app.trading.orders import OrderRequest
from app.trading.paper_broker import PaperBroker
from app.trading.portfolio import EquitySnapshot, Portfolio

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    signal: Signal
    status: str  # accepted | rejected | ignored
    reason: str
    order_id: str | None = None


class TradingEngine:
    def __init__(self, strategy: Strategy, risk: RiskManager, broker: PaperBroker,
                 portfolio: Portfolio, bus: EventBus, *, lookback_bars: int = 300,
                 internal_signals: bool = True, log: logging.Logger | None = None) -> None:
        self.log = log or logger
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.portfolio = portfolio
        self.bus = bus
        self.lookback_bars = lookback_bars
        self.internal_signals = internal_signals
        self.entries_enabled = True
        self._windows: dict[str, deque[Candle]] = {}
        self._last_price: dict[str, float] = {}
        self._last_analysis: dict[str, Analysis] = {}

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
        self._update_trailing(candle, analysis.atr)
        self._record_equity(candle.close_time)
        if analysis.signal is not None and allow_entries and self.internal_signals:
            return self.handle_signal(analysis.signal, candle.close_time)
        return None

    def _append(self, candle: Candle) -> bool:
        window = self._windows.setdefault(candle.symbol, deque(maxlen=self.window_size))
        if window and candle.open_time <= window[-1].open_time:
            return False
        window.append(candle)
        return True

    # -- Signaux ----------------------------------------------------------------------------
    def handle_signal(self, signal: Signal, now: datetime | None = None) -> SignalOutcome:
        self.log.info("Signal %s détecté sur %s (%s) — %s", signal.direction, signal.symbol,
                    signal.source, signal.reason)
        outcome = self._decide(signal, now or signal.timestamp)
        payload = to_payload(signal) | {"status": outcome.status, "status_reason": outcome.reason,
                                        "order_id": outcome.order_id}
        self.bus.publish(EventType.SIGNAL, payload)
        return outcome

    def _decide(self, signal: Signal, now: datetime) -> SignalOutcome:
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
        decision = self.risk.evaluate(signal, self._account(exclude=signal.symbol if reversing else None))
        if not decision.approved:
            return SignalOutcome(signal, "rejected", decision.reason)
        order = self.broker.submit_order(OrderRequest(
            symbol=signal.symbol, side=signal.direction.entry_side, quantity=decision.quantity,
            reference_price=signal.price, stop_distance=decision.stop_distance,
            take_profit_distance=decision.take_profit_distance, strategy=signal.strategy,
            reason=signal.reason,
        ), now)
        return SignalOutcome(signal, "accepted", "Ordre créé", order.id)

    def _account(self, exclude: str | None) -> AccountSnapshot:
        positions = [p for s, p in self.portfolio.positions.items() if s != exclude]
        pending = [o for o in self.broker.pending_orders() if o.intent is OrderIntent.OPEN]
        reserved = (sum(p.cost_basis for p in positions)
                    + sum(o.quantity * (o.reference_price or 0.0) for o in pending))
        return AccountSnapshot(equity=self.portfolio.equity(),
                               available_cash=self.portfolio.balance - reserved,
                               open_positions=len(positions) + len(pending))

    # -- Gestion des positions et du risque ---------------------------------------------------
    def _update_trailing(self, candle: Candle, atr: float | None) -> None:
        exits = self.risk.exit_params
        position = self.portfolio.position(candle.symbol)
        if not exits.trailing_stop_enabled or position is None or atr is None:
            return
        new_stop = trailing_stop_level(position, candle.close, atr, exits)
        if new_stop is not None:
            self.broker.modify_stop(candle.symbol, new_stop, candle.close_time,
                                    f"clôture {candle.close:.8g}, ATR {atr:.8g}")

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
