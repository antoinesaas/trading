"""Vérifie qu'aucun chemin de code ne permet d'envoyer un ordre réel."""

import re
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.core.events import EventBus
from app.market.data_provider import BinanceMarketDataProvider, ReadOnlyHttpClient
from app.risk.risk_manager import RiskLimits
from app.safety import (
    LiveTradingDisabledError, assert_paper_only, detect_live_credentials, ensure_paper_broker,
)
from app.services import build_services
from app.trading.factory import create_broker
from app.trading.paper_broker import PaperBroker
from app.trading.portfolio import Portfolio
from app.trading.wallet_broker import WalletBroker
from tests.conftest import StaticProvider, make_settings

ROOT = Path(__file__).resolve().parent.parent


def test_paper_only_false_is_refused():
    with pytest.raises(LiveTradingDisabledError):
        Settings(_env_file=None, paper_only=False)


def test_live_trading_mode_is_refused():
    with pytest.raises(LiveTradingDisabledError):
        Settings(_env_file=None, trading_mode="live")


def test_environment_cannot_enable_live(monkeypatch):
    monkeypatch.setenv("PAPER_ONLY", "false")
    with pytest.raises(LiveTradingDisabledError):
        Settings(_env_file=None)


def test_env_file_cannot_enable_live(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PAPER_ONLY=true\nTRADING_MODE=live\n", encoding="utf-8")
    with pytest.raises(LiveTradingDisabledError):
        Settings(_env_file=env)


def test_assert_paper_only_checks_any_object():
    class Fake:
        paper_only, trading_mode = True, "live"
    with pytest.raises(LiveTradingDisabledError):
        assert_paper_only(Fake())


def test_wallet_broker_cannot_be_used():
    assert WalletBroker.is_live is True
    with pytest.raises(LiveTradingDisabledError):
        WalletBroker()


def test_broker_factory_only_builds_paper_brokers():
    args = (Portfolio(1_000.0), EventBus(), RiskLimits())
    assert isinstance(create_broker("paper", *args), PaperBroker)
    for kind in ("live", "wallet", "metamask", "binance"):
        with pytest.raises(LiveTradingDisabledError):
            create_broker(kind, *args)


def test_ensure_paper_broker_refuses_live_or_undeclared_brokers():
    class LiveBroker:
        is_live = True

    class Undeclared:
        pass

    for broker in (LiveBroker(), Undeclared()):
        with pytest.raises(LiveTradingDisabledError):
            ensure_paper_broker(broker)


def test_live_credentials_are_detected():
    env = {"BINANCE_API_SECRET": "x", "WALLET_PRIVATE_KEY": "x", "METAMASK_MNEMONIC": "x",
           "ANTHROPIC_API_KEY": "x", "WEBHOOK_SECRET": "x", "DASHBOARD_TOKEN": "x", "DATABASE_URL": "x"}
    assert detect_live_credentials(env) == ["BINANCE_API_SECRET", "METAMASK_MNEMONIC", "WALLET_PRIVATE_KEY"]


def test_services_refuse_to_start_with_live_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "should-not-be-here")
    settings = make_settings(tmp_path)
    with pytest.raises(LiveTradingDisabledError):
        build_services(settings, provider=StaticProvider(settings.symbol_list), env_file=None)


def test_services_refuse_live_credentials_in_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SEED_PHRASE=abandon abandon\n", encoding="utf-8")
    settings = make_settings(tmp_path)
    with pytest.raises(LiveTradingDisabledError):
        build_services(settings, provider=StaticProvider(settings.symbol_list), env_file=env)


def test_market_data_client_is_read_only():
    client = ReadOnlyHttpClient("https://example.invalid")
    assert not any(hasattr(client, m) for m in ("post", "put", "delete", "patch"))
    for path in ("/api/v3/order", "/api/v3/order/test", "/sapi/v1/capital/withdraw/apply"):
        with pytest.raises(LiveTradingDisabledError):
            client.get(path, {})


def test_market_data_provider_only_sends_get_klines():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=[[0, "1", "2", "0.5", "1.5", "10", 3_599_999]])

    provider = BinanceMarketDataProvider("https://market.test", transport=httpx.MockTransport(handler))
    candles = provider.fetch_candles("BTCUSDT", "1h", 1)
    assert candles[0].close == 1.5
    assert seen == [("GET", "/api/v3/klines")]


FORBIDDEN = [
    r"eth_sendTransaction", r"eth_sendRawTransaction", r"eth_sign", r"personal_sign", r"wallet_sendCalls",
    r"/api/v3/order", r"/fapi/", r"import ccxt", r"from web3", r"import web3", r"create_order\(",
    r"signTransaction", r"privateKey",
]


def test_codebase_contains_no_real_order_code():
    files = [p for p in (ROOT / "app").rglob("*.py")] + list((ROOT / "dashboard").glob("*.*"))
    offenders = [f"{path.name}: {pattern}" for path in files for pattern in FORBIDDEN
                 if re.search(pattern, path.read_text(encoding="utf-8"))]
    assert offenders == []


def test_dashboard_has_no_live_trading_button():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8").lower()
    actions = re.findall(r'data-action="([^"]+)"', html)
    assert sorted(actions) == ["pause", "reset-halt", "start", "stop"]
    assert "live" not in " ".join(actions)
