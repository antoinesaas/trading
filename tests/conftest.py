"""Fixtures communes. Aucun test n'accède au réseau."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.events import EventBus, EventCollector
from app.core.types import Candle
from app.engine.params import ParameterSet
from app.engine.stack import TradingStack, build_trading_stack
from app.main import create_app
from app.market.data_provider import MarketDataProvider, generate_synthetic_candles
from app.risk.risk_manager import RiskLimits
from app.services import Services, build_services

WEBHOOK_SECRET = "test-webhook-secret-0123456789"
DASHBOARD_TOKEN = "test-dashboard-token-0123456789"
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def candle(open_: float, high: float, low: float, close: float, index: int = 0, symbol: str = "TEST",
           timeframe: str = "1h", closed: bool = True) -> Candle:
    start = T0 + timedelta(hours=index)
    return Candle(symbol, timeframe, start, start + timedelta(hours=1), open_, high, low, close, 100.0, closed)


def series_from_closes(closes: list[float], symbol: str = "TEST") -> list[Candle]:
    out, prev = [], closes[0]
    for i, close in enumerate(closes):
        out.append(candle(prev, max(prev, close) + 0.1, min(prev, close) - 0.1, close, i, symbol))
        prev = close
    return out


class StaticProvider(MarketDataProvider):
    """Fournisseur hors ligne : bougies synthétiques se terminant maintenant."""

    name = "static"

    def __init__(self, symbols: list[str], timeframe: str = "15m", bars: int = 400) -> None:
        step = timedelta(minutes=15)
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        end = now - timedelta(minutes=now.minute % 15)
        self.candles = {
            s: generate_synthetic_candles(s, timeframe, bars, seed=i + 1, start_price=100.0 * (i + 1),
                                          start_time=end - bars * step)
            for i, s in enumerate(symbols)
        }

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        return self.candles[symbol][-limit:]


def zero_cost_limits(**overrides: Any) -> RiskLimits:
    base: dict[str, Any] = {"fee_rate": 0.0, "slippage_rate": 0.0, "min_notional": 0.0}
    return RiskLimits(**(base | overrides))


@pytest.fixture
def collector() -> EventCollector:
    return EventCollector()


@pytest.fixture
def make_stack(collector: EventCollector) -> Callable[..., TradingStack]:
    def factory(limits: RiskLimits | None = None, params: ParameterSet | None = None,
                capital: float = 10_000.0) -> TradingStack:
        bus = EventBus()
        bus.subscribe(collector)
        return build_trading_stack(params or ParameterSet(), limits or RiskLimits(), capital, bus=bus)
    return factory


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": f"sqlite:///{(tmp_path / 'bot.db').as_posix()}",
        "webhook_secret": WEBHOOK_SECRET,
        "dashboard_token": DASHBOARD_TOKEN,
        "symbols": "BTCUSDT,ETHUSDT",
        "timeframe": "15m",
        "signal_source": "tradingview",
        "poll_interval_seconds": 1,
        "log_dir": tmp_path / "logs",
    }
    return Settings(**(base | overrides))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def services(settings: Settings) -> Services:
    return build_services(settings, provider=StaticProvider(settings.symbol_list), env_file=None)


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    with TestClient(create_app(services=services)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {DASHBOARD_TOKEN}"}


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("Condition non atteinte dans le délai imparti")


@pytest.fixture
def running_bot(client: TestClient, services: Services, auth: dict[str, str]) -> TestClient:
    assert client.post("/api/bot/start", headers=auth).status_code == 200
    wait_for(lambda: services.runner.last_tick is not None)
    return client
