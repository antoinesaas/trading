from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.webhook import WebhookGuard, normalize_tv_interval
from app.main import create_app
from app.services import build_services
from tests.conftest import WEBHOOK_SECRET, StaticProvider, make_settings

URL = "/webhook/tradingview"


def payload(services, **overrides):
    symbol = overrides.get("symbol", "BTCUSDT")
    price = services.stack.engine.last_price(symbol) or 100.0
    base = {"secret": WEBHOOK_SECRET, "symbol": symbol, "action": "LONG", "price": price, "atr": price * 0.01,
            "timestamp": datetime.now(UTC).isoformat(), "timeframe": "15",
            "bar_time": int(datetime.now(UTC).timestamp() * 1000), "strategy": "ema_rsi_tv"}
    return base | overrides


def test_valid_signal_is_accepted(running_bot, services):
    response = running_bot.post(URL, json=payload(services))
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted" and body["order_id"].startswith("PO-")


def test_exchange_prefix_and_ms_timestamp_are_normalized(running_bot, services):
    data = payload(services, symbol="BINANCE:ETHUSDT", action="short",
                   timestamp=int(datetime.now(UTC).timestamp() * 1000))
    data["price"] = services.stack.engine.last_price("ETHUSDT")
    data["atr"] = data["price"] * 0.01
    response = running_bot.post(URL, json=data)
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("secret", ["wrong-secret-value-123456", "", None])
def test_wrong_or_missing_secret_is_refused(running_bot, services, secret):
    data = payload(services)
    if secret is None:
        data.pop("secret")
    else:
        data["secret"] = secret
    assert running_bot.post(URL, json=data).status_code == 401


def test_invalid_json_and_payloads_are_refused(running_bot, services):
    assert running_bot.post(URL, content=b"{not json", headers={"Content-Type": "application/json"}).status_code == 400
    assert running_bot.post(URL, json=["list"]).status_code == 400
    assert running_bot.post(URL, json=payload(services, action="BUY")).status_code == 422
    assert running_bot.post(URL, json=payload(services, price=-5)).status_code == 422
    assert running_bot.post(URL, json=payload(services, atr=0)).status_code == 422
    assert running_bot.post(URL, json=payload(services, unexpected="x")).status_code == 422
    missing = payload(services)
    missing.pop("price")
    assert running_bot.post(URL, json=missing).status_code == 422
    assert running_bot.post(URL, content=b"x" * 5000).status_code == 413


def test_symbol_timestamp_timeframe_and_price_are_validated(running_bot, services):
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    assert running_bot.post(URL, json=payload(services, symbol="DOGEUSDT")).status_code == 422
    assert running_bot.post(URL, json=payload(services, timestamp=old)).status_code == 422
    assert running_bot.post(URL, json=payload(services, timestamp=future)).status_code == 422
    assert running_bot.post(URL, json=payload(services, timeframe="60")).status_code == 422
    far_price = services.stack.engine.last_price("BTCUSDT") * 1.5
    assert running_bot.post(URL, json=payload(services, price=far_price)).status_code == 422


def test_duplicate_signal_is_refused(running_bot, services):
    data = payload(services, bar_time=1_700_000_000_000)
    assert running_bot.post(URL, json=data).status_code == 202
    assert running_bot.post(URL, json=data).status_code == 409


def test_secret_is_never_persisted(running_bot, services):
    running_bot.post(URL, json=payload(services, symbol="DOGEUSDT"))
    running_bot.post(URL, json=payload(services))
    for row in services.repo.recent_signals(50):
        assert WEBHOOK_SECRET not in str(row)


def test_bot_must_be_running(client, services):
    response = client.post(URL, json=payload(services))
    assert response.status_code == 409 and "non démarré" in response.json()["detail"]


def test_webhook_disabled_with_insecure_secret(tmp_path):
    settings = make_settings(tmp_path, webhook_secret="change_me")
    services = build_services(settings, provider=StaticProvider(settings.symbol_list), env_file=None)
    with TestClient(create_app(services=services)) as client:
        response = client.post(URL, json={"secret": "change_me"})
    assert response.status_code == 503


def test_tradingview_signals_disabled_in_internal_mode(tmp_path):
    settings = make_settings(tmp_path, signal_source="internal")
    services = build_services(settings, provider=StaticProvider(settings.symbol_list), env_file=None)
    with TestClient(create_app(services=services)) as client:
        response = client.post(URL, json=payload(services))
    assert response.status_code == 409 and "SIGNAL_SOURCE" in response.json()["detail"]


def test_rate_limit_and_interval_normalization():
    guard = WebhookGuard(rate_per_minute=2, dedupe_ttl_seconds=60)
    assert guard.allow("ip", 0) and guard.allow("ip", 1) and not guard.allow("ip", 2)
    assert guard.allow("ip", 61.5)
    assert not guard.is_duplicate("k", 0) and guard.is_duplicate("k", 1) and not guard.is_duplicate("k", 100)
    assert [normalize_tv_interval(v) for v in ("15", "60", "240", "D", "1D", "15m")] == \
        ["15m", "1h", "4h", "1d", "1d", "15m"]
