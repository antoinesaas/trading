"""Flux de marché permanent et états du bot.

Le flux de prix tourne dès le démarrage du serveur, que le bot soit en marche ou non :
les graphiques sont en direct et les stops / objectifs des positions ouvertes sont
toujours gérés (comme des ordres stop laissés chez un broker).

États (ils ne concernent que les NOUVELLES décisions) :
- RUNNING : Claude analyse les opportunités et peut ouvrir des positions ;
- PAUSED  : aucune nouvelle entrée, les positions restent gérées ;
- STOPPED : idem, et aucune analyse IA ;
- HALTED  : kill-switch drawdown déclenché (réinitialisation manuelle requise).

Cadence : crypto à chaque cycle (``poll_interval``), marchés à horaires (actions, ETF,
forex) toutes les 60 s quand ils sont ouverts et toutes les 15 min quand ils sont fermés.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.events import EventBus, EventType
from app.core.types import Candle, ensure_utc, utcnow
from app.database.repository import TradingRepository
from app.engine.trading_engine import TradingEngine
from app.market.data_provider import BINANCE_MAX_LIMIT, MarketDataError, MarketDataProvider
from app.market.sessions import market_open
from app.market.timeframes import timeframe_seconds
from app.market.universe import instrument

logger = logging.getLogger(__name__)
OPEN_MARKET_POLL = timedelta(seconds=60)
CLOSED_MARKET_POLL = timedelta(minutes=15)
ERROR_RETRY = timedelta(seconds=60)  # nouvel essai après une erreur de données


class BotStatus(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"


class BotStateError(RuntimeError):
    """Transition d'état refusée."""


def candle_message(candle: Candle) -> dict[str, Any]:
    return {"symbol": candle.symbol, "time": int(candle.open_time.timestamp()), "open": candle.open,
            "high": candle.high, "low": candle.low, "close": candle.close, "volume": candle.volume,
            "closed": candle.closed}


class BotRunner:
    def __init__(self, engine: TradingEngine, provider: MarketDataProvider, bus: EventBus, *,
                 symbols: list[str], timeframe: str, poll_interval: float,
                 repository: TradingRepository | None = None,
                 last_processed: dict[str, str] | None = None,
                 after_tick: Callable[[list[str]], Awaitable[None]] | None = None) -> None:
        self.engine = engine
        self.provider = provider
        self.bus = bus
        self.symbols = symbols
        self.timeframe = timeframe
        self.poll_interval = poll_interval
        self.repository = repository
        self.after_tick = after_tick
        self._tf = timedelta(seconds=timeframe_seconds(timeframe))
        self._status = BotStatus.STOPPED
        self._task: asyncio.Task[None] | None = None
        self._warmed: set[str] = set()
        self._next_poll: dict[str, datetime] = {}
        self._last_processed = dict(last_processed or {})
        self.forming: dict[str, Candle] = {}
        self.symbol_errors: dict[str, str] = {}
        self._scan_all = False  # au démarrage : tous les marchés sont examinés une fois
        self.last_tick: datetime | None = None
        self.last_error: str | None = None
        self.engine.entries_enabled = False

    # -- État ------------------------------------------------------------------------
    @property
    def status(self) -> BotStatus:
        return BotStatus.HALTED if self.engine.risk.halted else self._status

    @property
    def is_running(self) -> bool:
        return self.status is BotStatus.RUNNING

    @property
    def feed_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_feed(self) -> None:
        """Démarre le flux de marché (appelé au lancement du serveur)."""
        if not self.feed_running:
            self._task = asyncio.create_task(self._loop(), name="market-feed")

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def start(self) -> BotStatus:
        if self.engine.risk.halted:
            raise BotStateError("Kill-switch actif : réinitialisez-le avant de redémarrer.")
        if self._status is BotStatus.RUNNING:
            return self.status
        self._status = BotStatus.RUNNING
        self.engine.entries_enabled = True
        self._scan_all = True
        self.start_feed()
        self._announce("Bot démarré (paper trading) : Claude analyse les marchés ouverts")
        return self.status

    async def pause(self) -> BotStatus:
        if self._status is not BotStatus.RUNNING:
            raise BotStateError("Seul un bot en marche peut être mis en pause.")
        self._status = BotStatus.PAUSED
        self.engine.entries_enabled = False
        self._announce("Bot en pause : stops et objectifs toujours gérés, aucune nouvelle entrée")
        return self.status

    async def stop(self) -> BotStatus:
        self._status = BotStatus.STOPPED
        self.engine.entries_enabled = False
        self._announce("Bot arrêté : plus de nouvelles décisions ; les stops restent surveillés")
        return self.status

    def reset_halt(self) -> BotStatus:
        self.engine.risk.reset_halt()
        if self.repository is not None:
            self.repository.set_state("risk_halt", {"halted": False, "reason": None})
        self._announce("Kill-switch réinitialisé manuellement", level="WARNING")
        return self.status

    def _announce(self, message: str, level: str = "INFO") -> None:
        logger.log(getattr(logging, level), message)
        self.bus.publish(EventType.BOT, {"level": level, "message": message, "status": self.status.value})

    # -- Boucle -----------------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - la boucle doit survivre aux erreurs réseau
                self.last_error = str(exc)
                logger.exception("Erreur dans la boucle du bot")
                self.bus.publish(EventType.BOT, {"level": "ERROR", "message": f"Erreur : {exc}",
                                                 "status": self.status.value})
            await asyncio.sleep(self.poll_interval)

    def _due(self, symbol: str, now: datetime) -> bool:
        return now >= self._next_poll.get(symbol, now)

    def _schedule(self, symbol: str, now: datetime) -> None:
        calendar = instrument(symbol).calendar
        if calendar == "24/7":
            self._next_poll[symbol] = now
        else:
            self._next_poll[symbol] = now + (OPEN_MARKET_POLL if market_open(calendar, now) else CLOSED_MARKET_POLL)

    async def tick(self) -> None:
        now = utcnow()
        due = [s for s in self.symbols if self._due(s, now)]
        results = await asyncio.gather(*(self._fetch_for(s) for s in due), return_exceptions=True)
        fresh: list[str] = []
        for symbol, result in zip(due, results, strict=True):
            self._schedule(symbol, now)
            if isinstance(result, BaseException):
                if not isinstance(result, MarketDataError | OSError | ValueError):
                    raise result
                self._next_poll[symbol] = now + ERROR_RETRY  # source en panne : ne pas la marteler
                self.symbol_errors[symbol] = str(result)
                logger.warning("%s : données indisponibles (%s)", symbol, result)
                continue
            self.symbol_errors.pop(symbol, None)
            if self._process(symbol, result):
                fresh.append(symbol)
        self.last_tick = utcnow()
        self._persist_progress()
        if self.after_tick is not None and self._status is BotStatus.RUNNING:
            if self._scan_all:  # premier cycle après Start : sans attendre la prochaine clôture
                self._scan_all = False
                fresh = [s for s in self.symbols if s in self._warmed and s not in self.symbol_errors]
            await self.after_tick(fresh)

    async def _fetch_for(self, symbol: str) -> list[Candle]:
        limit = self.engine.window_size + 1 if symbol not in self._warmed else \
            self._fetch_limit(self.engine.last_closed_time(symbol))
        return await asyncio.to_thread(self.provider.fetch_candles, symbol, self.timeframe, limit)

    def _warm_up(self, symbol: str, candles: list[Candle]) -> None:
        closed = [c for c in candles if c.closed]
        raw_cutoff = self._last_processed.get(symbol)
        cutoff = ensure_utc(datetime.fromisoformat(raw_cutoff)) if raw_cutoff else None
        history = [c for c in closed if cutoff is None or c.open_time <= cutoff]
        missed = [c for c in closed if cutoff is not None and c.open_time > cutoff]
        self.engine.warm_up(history)
        for candle in missed:  # rattrapage des sorties manquées pendant l'arrêt, sans nouvelle entrée
            self.engine.on_bar_update(candle)
            self.engine.on_bar_close(candle, allow_entries=False)
        self._warmed.add(symbol)
        logger.info("%s : %d bougies chargées, %d rattrapées", symbol, len(history), len(missed))

    def _process(self, symbol: str, candles: list[Candle]) -> bool:
        """Traite les nouvelles bougies ; retourne ``True`` si une bougie récente vient de clôturer."""
        if symbol not in self._warmed:
            self._warm_up(symbol, candles)
        last = self.engine.last_closed_time(symbol)
        closed = [c for c in candles if c.closed and (last is None or c.open_time > last)]
        now = utcnow()
        fresh = False
        for index, candle in enumerate(closed):
            fresh = now - candle.close_time <= self._tf
            self.engine.on_bar_update(candle)
            self.engine.on_bar_close(candle, allow_entries=index == len(closed) - 1 and fresh)
            self.bus.publish(EventType.CANDLE, candle_message(candle))
        if candles and not candles[-1].closed:
            self.forming[symbol] = candles[-1]
            self.engine.on_bar_update(candles[-1])
            self.bus.publish(EventType.CANDLE, candle_message(candles[-1]))
        return fresh

    def _fetch_limit(self, last: datetime | None) -> int:
        if last is None:
            return self.engine.window_size + 1
        missed = int((utcnow() - last) / self._tf) + 3
        if missed > BINANCE_MAX_LIMIT:
            logger.warning("%d bougies manquées : seules les %d dernières sont rattrapées",
                           missed, BINANCE_MAX_LIMIT)
        return max(3, min(missed, BINANCE_MAX_LIMIT))

    def _persist_progress(self) -> None:
        progress = {s: t.isoformat() for s in self.symbols if (t := self.engine.last_closed_time(s))}
        if progress != self._last_processed and self.repository is not None:
            self.repository.set_state("last_processed", progress)
        self._last_processed = progress
