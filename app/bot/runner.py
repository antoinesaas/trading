"""Boucle temps réel du bot : interroge les données de marché et alimente le moteur.

États : STOPPED (boucle arrêtée), RUNNING (entrées autorisées), PAUSED (boucle active :
les stops/objectifs restent gérés, mais aucune nouvelle entrée), HALTED (kill-switch
drawdown déclenché : réinitialisation manuelle requise).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.events import EventBus, EventType
from app.core.types import Candle, ensure_utc, utcnow
from app.database.repository import TradingRepository
from app.engine.trading_engine import TradingEngine
from app.market.data_provider import BINANCE_MAX_LIMIT, MarketDataProvider
from app.market.timeframes import timeframe_seconds

logger = logging.getLogger(__name__)


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
                 last_processed: dict[str, str] | None = None) -> None:
        self.engine = engine
        self.provider = provider
        self.bus = bus
        self.symbols = symbols
        self.timeframe = timeframe
        self.poll_interval = poll_interval
        self.repository = repository
        self._tf = timedelta(seconds=timeframe_seconds(timeframe))
        self._status = BotStatus.STOPPED
        self._task: asyncio.Task[None] | None = None
        self._warmed: set[str] = set()
        self._last_processed = dict(last_processed or {})
        self.forming: dict[str, Candle] = {}
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

    async def start(self) -> BotStatus:
        if self.engine.risk.halted:
            raise BotStateError("Kill-switch actif : réinitialisez-le avant de redémarrer.")
        if self._status is BotStatus.RUNNING:
            return self.status
        self._status = BotStatus.RUNNING
        self.engine.entries_enabled = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="bot-loop")
        self._announce("Bot démarré (paper trading)")
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
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._announce("Bot arrêté : positions ouvertes conservées, rattrapage au prochain démarrage")
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

    async def tick(self) -> None:
        for symbol in self.symbols:
            if symbol not in self._warmed:
                await self._warm_up(symbol)
            await self._process(symbol)
        self.last_tick = utcnow()
        self._persist_progress()

    async def _fetch(self, symbol: str, limit: int) -> list[Candle]:
        return await asyncio.to_thread(self.provider.fetch_candles, symbol, self.timeframe, limit)

    async def _warm_up(self, symbol: str) -> None:
        candles = [c for c in await self._fetch(symbol, self.engine.window_size + 1) if c.closed]
        raw_cutoff = self._last_processed.get(symbol)
        cutoff = ensure_utc(datetime.fromisoformat(raw_cutoff)) if raw_cutoff else None
        history = [c for c in candles if cutoff is None or c.open_time <= cutoff]
        missed = [c for c in candles if cutoff is not None and c.open_time > cutoff]
        self.engine.warm_up(history)
        for candle in missed:  # rattrapage des sorties manquées pendant l'arrêt, sans nouvelle entrée
            self.engine.on_bar_update(candle)
            self.engine.on_bar_close(candle, allow_entries=False)
        self._warmed.add(symbol)
        logger.info("%s : %d bougies chargées, %d rattrapées", symbol, len(history), len(missed))

    async def _process(self, symbol: str) -> None:
        last = self.engine.last_closed_time(symbol)
        candles = await self._fetch(symbol, self._fetch_limit(last))
        closed = [c for c in candles if c.closed and (last is None or c.open_time > last)]
        now = utcnow()
        for index, candle in enumerate(closed):
            fresh = now - candle.close_time <= self._tf
            self.engine.on_bar_update(candle)
            self.engine.on_bar_close(candle, allow_entries=index == len(closed) - 1 and fresh)
            self.bus.publish(EventType.CANDLE, candle_message(candle))
        if candles and not candles[-1].closed:
            self.forming[symbol] = candles[-1]
            self.engine.on_bar_update(candles[-1])
            self.bus.publish(EventType.CANDLE, candle_message(candles[-1]))

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
