import pytest
from fastapi.websockets import WebSocketDisconnect

from tests.conftest import DASHBOARD_TOKEN, wait_for


def test_health_is_public_and_paper(client):
    body = client.get("/health").json()
    assert body["paper_only"] is True and body["mode"] == "paper"


def test_api_requires_dashboard_token(client, auth):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.get("/api/status", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "PAPER" and body["live_trading_available"] is False
    assert body["account"]["initial_capital"] == 10_000
    for key in ("balance", "equity", "pnl", "pnl_pct", "drawdown"):
        assert key in body["account"]
    for key in ("trades", "win_rate", "profit_factor"):
        assert key in body["performance"]


def test_start_pause_stop_cycle(client, services, auth):
    assert client.post("/api/bot/pause", headers=auth).status_code == 409
    assert client.post("/api/bot/start", headers=auth).json()["status"] == "RUNNING"
    wait_for(lambda: services.runner.last_tick is not None)
    assert client.post("/api/bot/pause", headers=auth).json()["status"] == "PAUSED"
    assert services.stack.engine.entries_enabled is False
    assert client.post("/api/bot/start", headers=auth).json()["status"] == "RUNNING"
    assert client.post("/api/bot/stop", headers=auth).json()["status"] == "STOPPED"


def test_candles_and_history_endpoints(running_bot, auth):
    candles = running_bot.get("/api/candles?symbol=BTCUSDT", headers=auth).json()
    assert len(candles["candles"]) > 100
    assert len(candles["indicators"]) == 2
    assert running_bot.get("/api/candles?symbol=NOPE", headers=auth).status_code == 404
    for path in ("/api/trades", "/api/orders", "/api/signals", "/api/events", "/api/equity", "/api/optimizer"):
        assert running_bot.get(path, headers=auth).status_code == 200
    config = running_bot.get("/api/config", headers=auth).json()
    assert "webhook_secret" not in config and "dashboard_token" not in config and config["paper_only"] is True


def test_live_mode_request_is_blocked(client, services, auth):
    assert client.get("/api/mode", headers=auth).json()["live_trading_available"] is False
    response = client.put("/api/mode", json={"mode": "live"}, headers=auth)
    assert response.status_code == 403
    events = services.repo.recent_events(10)
    assert any(e["level"] == "CRITICAL" and e["event_type"] == "security" for e in events)


def test_optimizer_unavailable_without_api_key(client, auth):
    assert client.post("/api/optimizer/run", headers=auth).status_code == 503


def test_websocket_requires_token_and_streams_status(client):
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=bad") as ws:
        ws.receive_json()
    with client.websocket_connect(f"/ws?token={DASHBOARD_TOKEN}") as ws:
        for _ in range(20):
            message = ws.receive_json()
            if message["type"] == "status":
                assert message["data"]["paper_only"] is True
                break
        else:
            pytest.fail("aucun statut reçu")


def test_dashboard_is_served(client):
    response = client.get("/dashboard/")
    assert response.status_code == 200 and "PAPER" in response.text
