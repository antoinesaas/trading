"""API du dashboard (protégée par ``DASHBOARD_TOKEN``) et WebSocket temps réel.

Aucun endpoint ne permet d'activer le trading réel : ``PUT /api/mode`` répond
toujours 403 pour tout mode autre que ``paper``.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel

from app.ai.service import OptimizerBusyError, OptimizerUnavailableError
from app.backtest.metrics import compute_metrics
from app.bot.runner import BotStateError
from app.core.types import to_payload, utcnow
from app.market.data_provider import MarketDataError
from app.safety import reject_live_mode_request
from app.services import Services
from app.strategy import CandleWindow

logger = logging.getLogger(__name__)
_background: set[asyncio.Task[Any]] = set()


def _services(request: Request) -> Services:
    return request.app.state.services


def _token_ok(services: Services, provided: str | None) -> bool:
    expected = services.settings.dashboard_token.get_secret_value()
    return bool(expected and provided) and hmac.compare_digest(provided.encode(), expected.encode())


def require_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> Services:
    services = _services(request)
    if not services.settings.dashboard_token.get_secret_value():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DASHBOARD_TOKEN non configuré")
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    if not _token_ok(services, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton du dashboard invalide",
                            headers={"WWW-Authenticate": "Bearer"})
    return services


Authed = Annotated[Services, Depends(require_token)]
router = APIRouter(prefix="/api")


# --- Lecture de l'état ---------------------------------------------------------------------

def build_status(services: Services) -> dict[str, Any]:
    s, engine, runner = services.settings, services.stack.engine, services.runner
    portfolio, risk = services.stack.portfolio, services.stack.risk
    equity = portfolio.equity()
    pnl = equity - portfolio.initial_capital
    perf = compute_metrics(portfolio.trades, [equity], portfolio.initial_capital, 1.0)
    positions = [to_payload(p) | {"unrealized_pnl": p.unrealized_pnl(),
                                  "unrealized_pct": p.unrealized_pnl() / p.cost_basis}
                 for p in portfolio.positions.values()]
    return {
        "server_time": utcnow().isoformat(),
        "mode": "PAPER",
        "paper_only": True,
        "live_trading_available": False,
        "bot": {"status": runner.status.value, "symbols": s.symbol_list, "timeframe": s.timeframe,
                "signal_source": s.signal_source, "poll_interval": s.poll_interval_seconds,
                "last_tick": runner.last_tick.isoformat() if runner.last_tick else None,
                "last_error": runner.last_error, "clients": services.broadcaster.clients},
        "account": {"currency": s.account_currency, "initial_capital": portfolio.initial_capital,
                    "balance": portfolio.balance, "equity": equity,
                    "available_cash": portfolio.available_cash(),
                    "unrealized_pnl": portfolio.unrealized_pnl(),
                    "realized_pnl": portfolio.realized_pnl(), "pnl": pnl,
                    "pnl_pct": pnl / portfolio.initial_capital, "drawdown": portfolio.drawdown(),
                    "peak_equity": max(portfolio.peak_equity, equity)},
        "performance": {k: getattr(perf, k) for k in (
            "trades", "wins", "losses", "win_rate", "avg_win", "avg_loss", "profit_factor",
            "expectancy", "expectancy_r", "fees_paid")},
        "risk": risk.status() | {"limits": asdict(risk.limits)},
        "positions": positions,
        "pending_orders": to_payload(engine.broker.pending_orders()),
        "prices": {sym: engine.last_price(sym) for sym in s.symbol_list},
        "params": engine.current_params().model_dump(mode="json"),
        "optimizer": {"available": services.optimizer.available,
                      "scheduled": services.optimizer.scheduled,
                      "running": services.optimizer.running, "model": services.optimizer.model_name,
                      "auto_apply": services.optimizer.auto_apply,
                      "interval_hours": services.optimizer.interval_hours},
    }


@router.get("/status")
async def get_status(services: Authed) -> dict[str, Any]:
    return build_status(services)


@router.get("/trades")
def get_trades(services: Authed, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> list[dict[str, Any]]:
    return services.repo.recent_trades(limit)


@router.get("/orders")
def get_orders(services: Authed, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> list[dict[str, Any]]:
    return services.repo.recent_orders(limit)


@router.get("/signals")
def get_signals(services: Authed, limit: Annotated[int, Query(ge=1, le=1000)] = 50) -> list[dict[str, Any]]:
    return services.repo.recent_signals(limit)


@router.get("/events")
def get_events(services: Authed, limit: Annotated[int, Query(ge=1, le=1000)] = 100) -> list[dict[str, Any]]:
    return services.repo.recent_events(limit)


@router.get("/equity")
def get_equity(services: Authed, limit: Annotated[int, Query(ge=1, le=20_000)] = 2_000) -> list[dict[str, Any]]:
    return services.repo.equity_curve(limit)


@router.get("/candles")
async def get_candles(services: Authed, symbol: str) -> dict[str, Any]:
    engine = services.stack.engine
    if symbol not in services.settings.symbol_list:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Symbole inconnu : {symbol}")
    candles, forming = engine.window(symbol), services.runner.forming.get(symbol)
    if not candles:  # bot arrêté : afficher quand même le marché
        try:
            fetched = await asyncio.to_thread(services.provider.fetch_candles, symbol,
                                              services.settings.timeframe, engine.window_size + 1)
        except MarketDataError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        candles = [c for c in fetched if c.closed]
        forming = fetched[-1] if fetched and not fetched[-1].closed else None
    rows = [{"time": int(c.open_time.timestamp()), "open": c.open, "high": c.high, "low": c.low,
             "close": c.close} for c in candles]
    if forming is not None and (not candles or forming.open_time > candles[-1].open_time):
        rows.append({"time": int(forming.open_time.timestamp()), "open": forming.open,
                     "high": forming.high, "low": forming.low, "close": forming.close})
    overlays: dict[str, list[dict[str, float]]] = {}
    if candles:
        series = engine.strategy.indicator_series(CandleWindow.from_candles(candles))
        for name, values in series.items():
            if name.startswith("ema"):
                overlays[name] = [{"time": r["time"], "value": float(v)}
                                  for r, v in zip(rows, values, strict=False) if v == v]
    return {"symbol": symbol, "timeframe": services.settings.timeframe, "candles": rows,
            "indicators": overlays, "trades": services.repo.recent_trades(200, symbol=symbol)}


@router.get("/config")
def get_config(services: Authed) -> dict[str, Any]:
    s = services.settings
    hidden = {"webhook_secret", "dashboard_token", "anthropic_api_key", "database_url"}
    data = s.model_dump(mode="json", exclude=hidden)
    data["database_backend"] = services.db.backend
    data["webhook_secure"] = s.webhook_secret_is_secure
    return data


# --- Contrôle du bot -----------------------------------------------------------------------

async def _transition(services: Services, action: str) -> dict[str, Any]:
    runner = services.runner
    try:
        if action == "start":
            await runner.start()
        elif action == "pause":
            await runner.pause()
        elif action == "stop":
            await runner.stop()
        else:
            runner.reset_halt()
    except BotStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"status": runner.status.value}


@router.post("/bot/start")
async def bot_start(services: Authed) -> dict[str, Any]:
    return await _transition(services, "start")


@router.post("/bot/pause")
async def bot_pause(services: Authed) -> dict[str, Any]:
    return await _transition(services, "pause")


@router.post("/bot/stop")
async def bot_stop(services: Authed) -> dict[str, Any]:
    return await _transition(services, "stop")


@router.post("/bot/reset-halt")
async def bot_reset_halt(services: Authed) -> dict[str, Any]:
    return await _transition(services, "reset-halt")


class ModeRequest(BaseModel):
    mode: str


@router.get("/mode")
def get_mode(_: Authed) -> dict[str, Any]:
    return {"mode": "paper", "live_trading_available": False,
            "reason": "Version PAPER TRADING UNIQUEMENT : le trading réel est bloqué."}


@router.put("/mode")
def set_mode(request_body: ModeRequest, services: Authed) -> dict[str, Any]:
    if request_body.mode != "paper":
        error = reject_live_mode_request(request_body.mode)
        services.repo.insert_event("CRITICAL", "security", f"Tentative de passage en mode "
                                                           f"{request_body.mode!r} bloquée")
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error))
    return {"mode": "paper"}


# --- Optimiseur IA -------------------------------------------------------------------------

@router.get("/optimizer")
def get_optimizer(services: Authed) -> dict[str, Any]:
    return {"runs": services.repo.list_runs(10), "versions": services.repo.list_versions(20)}


@router.post("/optimizer/run", status_code=status.HTTP_202_ACCEPTED)
async def run_optimizer(services: Authed) -> dict[str, Any]:
    optimizer = services.optimizer
    if not optimizer.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Optimiseur IA indisponible : définir ANTHROPIC_API_KEY dans .env")
    if optimizer.running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Une optimisation est déjà en cours")

    async def run() -> None:
        try:
            await optimizer.run_once("manual")
        except (OptimizerBusyError, OptimizerUnavailableError) as exc:
            logger.warning("Optimisation non lancée : %s", exc)

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "started"}


@router.post("/optimizer/versions/{version_id}/activate")
async def activate_version(version_id: int, services: Authed) -> dict[str, Any]:
    try:
        params = services.optimizer.activate_version(version_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"activated": version_id, "params": params.model_dump(mode="json")}


# --- WebSocket ------------------------------------------------------------------------------

ws_router = APIRouter()


@ws_router.websocket("/ws")
async def websocket_feed(websocket: WebSocket, token: str = "") -> None:
    services: Services = websocket.app.state.services
    if not _token_ok(services, token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    queue = services.broadcaster.register()
    last_status = 0.0
    try:
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                await websocket.send_json(message)
            except TimeoutError:
                pass
            if time.monotonic() - last_status >= 1.0:
                await websocket.send_json({"type": "status", "data": build_status(services)})
                last_status = time.monotonic()
    except WebSocketDisconnect:
        pass
    finally:
        services.broadcaster.unregister(queue)
