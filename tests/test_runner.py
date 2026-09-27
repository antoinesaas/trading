"""Boucle du bot : flux de prix permanent, cadence par marché, isolement des erreurs de données."""

import asyncio

from app.bot.runner import BotRunner, BotStatus
from app.core.events import EventBus
from app.engine.params import ParameterSet
from app.engine.stack import build_trading_stack
from app.market.data_provider import MarketDataError
from tests.conftest import StaticProvider, zero_cost_limits


class FlakyProvider(StaticProvider):
    """Crypto disponible ; NVDA en panne (source Yahoo indisponible)."""

    def __init__(self) -> None:
        super().__init__(["BTCUSDT"])
        self.calls: list[str] = []

    def fetch_candles(self, symbol, timeframe, limit=500):
        self.calls.append(symbol)
        if symbol == "NVDA":
            raise MarketDataError("Yahoo indisponible")
        return super().fetch_candles(symbol, timeframe, limit)


def make_runner():
    provider = FlakyProvider()
    stack = build_trading_stack(ParameterSet(), zero_cost_limits(), 10_000, bus=EventBus())
    ticks: list[list[str]] = []

    async def after_tick(fresh):
        ticks.append(fresh)

    runner = BotRunner(stack.engine, provider, stack.bus, symbols=["BTCUSDT", "NVDA"], timeframe="15m",
                       poll_interval=1, after_tick=after_tick)
    return runner, provider, stack, ticks


def test_feed_runs_while_stopped_but_no_decision_is_taken():
    runner, provider, stack, ticks = make_runner()
    asyncio.run(runner.tick())
    assert runner.status is BotStatus.STOPPED
    assert stack.engine.last_price("BTCUSDT") is not None  # graphiques et stops alimentés
    assert ticks == [] and not stack.engine.entries_enabled  # aucune décision tant que le bot est arrêté
    assert stack.engine.last_analysis("BTCUSDT") is not None  # score du scanner dès le chargement


def test_one_broken_source_does_not_stop_the_other_markets():
    runner, provider, stack, _ = make_runner()
    asyncio.run(runner.tick())
    assert "NVDA" in runner.symbol_errors and "BTCUSDT" not in runner.symbol_errors
    assert runner.last_error is None and stack.engine.last_price("BTCUSDT") is not None


def test_non_crypto_markets_are_polled_less_often_than_crypto():
    runner, provider, _, _ = make_runner()
    asyncio.run(runner.tick())
    asyncio.run(runner.tick())
    assert provider.calls.count("BTCUSDT") == 2  # crypto : à chaque cycle
    assert provider.calls.count("NVDA") == 1  # bourse : au plus une fois par minute (15 min si fermée)


def test_a_failing_source_is_retried_after_a_delay_not_every_cycle():
    runner, provider, _, _ = make_runner()
    provider.candles["NVDA"] = provider.candles["BTCUSDT"]
    runner.symbols = ["NVDA"]
    asyncio.run(runner.tick())
    asyncio.run(runner.tick())
    assert provider.calls == ["NVDA"] and "NVDA" in runner.symbol_errors


def test_decisions_only_when_running():
    runner, _, stack, ticks = make_runner()

    async def scenario():
        await runner.start()
        await runner.shutdown()  # on pilote les cycles à la main
        await runner.tick()
        await runner.pause()
        await runner.tick()

    asyncio.run(scenario())
    assert len(ticks) == 1 and not stack.engine.entries_enabled
    assert ticks[0] == ["BTCUSDT"]  # au démarrage, les marchés disponibles sont examinés sans attendre
