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
from datetime import timedelta
from typing import Annotated, Any

import numpy as np

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel

from app.backtest.metrics import compute_metrics
from app.bot.runner import BotStateError
from app.core.events import EventType
from app.core.types import to_payload, utcnow
from app.market.data_provider import MarketDataError
from app.market.indicators import ema, rsi, swing_levels, zigzag
from app.market.sessions import market_clock, market_open
from app.market.universe import instrument
from app.readiness import evaluate_readiness
from app.safety import reject_live_mode_request
from app.services import Services

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
        "risk": risk.status(equity) | {"limits": asdict(risk.limits),
                                 "open_risk": asdict(engine.account().open_risk)},
        "positions": positions,
        "pending_orders": to_payload(engine.broker.pending_orders()),
        "prices": {sym: engine.last_price(sym) for sym in s.symbol_list},
        "params": engine.current_params().model_dump(mode="json"),
        "ai": {"enabled": services.ai_trader is not None, "mode": s.decision_mode,
               "decision_model": s.ai_decision_model, "review_model": s.ai_review_model,
               "briefing_model": s.ai_briefing_model, "min_confidence": s.ai_min_confidence,
               "min_confidence_effective": _threshold(services),
               "costs": services.costs.status(),
               "briefing": _briefing_summary(services)},
        "market_clock": market_clock(utcnow()).as_dict(),
        "optimizer": {"available": services.optimizer.available,
                      "scheduled": services.optimizer.scheduled,
                      "running": services.optimizer.running, "model": services.optimizer.model_name,
                      "auto_apply": services.optimizer.auto_apply,
                      "interval_hours": services.optimizer.interval_hours},
    }


def _threshold(services: Services) -> dict[str, Any] | None:
    if services.ai_trader is None:
        return None
    value, reason = services.ai_trader.threshold_status()
    return {"value": value, "reason": reason}


def _briefing_summary(services: Services) -> dict[str, Any] | None:
    briefing = services.briefing.latest if services.briefing else None
    if briefing is None:
        return None
    blackout = briefing.active_blackout(utcnow())
    return {"generated_at": briefing.generated_at.isoformat(), "risk_level": briefing.risk_level,
            "sentiment": briefing.overall_sentiment, "summary": briefing.summary,
            "events": [e.model_dump() for e in briefing.key_events],
            "per_symbol": [v.model_dump() for v in briefing.per_symbol],
            "blackout": blackout.model_dump(mode="json") if blackout else None,
            "blackouts": [w.model_dump(mode="json") for w in briefing.blackout_windows]}


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


CHART_TIMEFRAMES = ("1h", "4h", "1d", "1w")


@router.get("/chart")
async def get_chart(services: Authed, symbol: str, timeframe: str = "") -> dict[str, Any]:
    """Tout ce que le graphique affiche : bougies, volume, EMA, RSI, niveaux, structure, trades, IA."""
    if symbol not in services.settings.symbol_list:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Symbole inconnu : {symbol}")
    timeframe = timeframe or services.settings.timeframe
    if timeframe not in {*CHART_TIMEFRAMES, services.settings.timeframe}:
        raise HTTPException(422, f"Timeframe parmi {CHART_TIMEFRAMES}")
    candles = await _chart_candles(services, symbol, timeframe)
    spec = instrument(symbol)
    position = services.stack.portfolio.position(symbol)
    return {
        "symbol": symbol, "name": spec.name, "asset_class": spec.asset_class, "currency": spec.currency,
        "tradingview": spec.tradingview, "timeframe": timeframe,
        "market_open": market_open(spec.calendar, utcnow()),
        **chart_payload(candles),
        "trades": services.repo.recent_trades(300, symbol=symbol),
        "decisions": [d for d in services.repo.recent_decisions(300) if d["symbol"] == symbol],
        "position": to_payload(position) if position else None,
    }


async def _chart_candles(services: Services, symbol: str, timeframe: str) -> list[Any]:
    engine = services.stack.engine
    if timeframe == services.settings.timeframe and engine.window(symbol):
        candles = engine.window(symbol)
        forming = services.runner.forming.get(symbol)
        if forming is not None and forming.open_time > candles[-1].open_time:
            candles = [*candles, forming]
        return candles
    try:
        return await asyncio.to_thread(services.provider.fetch_candles, symbol, timeframe, 500)
    except MarketDataError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


def chart_payload(candles: list[Any]) -> dict[str, Any]:
    if not candles:
        return {"candles": [], "ema_fast": [], "ema_slow": [], "rsi": [], "levels": {"supports": [], "resistances": []},
                "structure": []}
    times = [int(c.open_time.timestamp()) for c in candles]
    close = np.array([c.close for c in candles])
    high, low = np.array([c.high for c in candles]), np.array([c.low for c in candles])

    def line(values: Any) -> list[dict[str, float]]:
        return [{"time": t, "value": round(float(v), 8)} for t, v in zip(times, values, strict=True) if v == v]

    recent = slice(-min(len(candles), 200), None)
    supports, resistances = swing_levels(high[recent], low[recent], lookback=5, count=3)
    offset = len(candles) - len(high[recent])
    return {
        "candles": [{"time": t, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                    for t, c in zip(times, candles, strict=True)],
        "ema_fast": line(ema(close, 20)), "ema_slow": line(ema(close, 50)), "rsi": line(rsi(close, 14)),
        "levels": {"supports": supports, "resistances": resistances},
        "structure": [{"time": times[offset + i], "value": price, "kind": kind}
                      for i, price, kind in zigzag(high[recent], low[recent], lookback=5)],
    }


@router.get("/watchlist")
async def get_watchlist(services: Authed) -> list[dict[str, Any]]:
    engine, now = services.stack.engine, utcnow()
    rows = []
    for symbol in services.settings.symbol_list:
        spec = instrument(symbol)
        window = engine.window(symbol)
        price = engine.last_price(symbol)
        cutoff = window[-1].open_time - timedelta(hours=24) if window else now
        reference = next((c.close for c in reversed(window) if c.open_time <= cutoff), None)
        position = services.stack.portfolio.position(symbol)
        analysis = engine.last_analysis(symbol)
        scores = [v for k, v in (analysis.indicators if analysis else {}).items() if k.startswith("score_")]
        rows.append({"symbol": symbol, "name": spec.name, "asset_class": spec.asset_class, "price": price,
                     "change_pct": (price / reference - 1) * 100 if price and reference else None,
                     "market_open": market_open(spec.calendar, now), "score": max(scores, default=None),
                     "position": position.direction.value if position else None,
                     "error": services.runner.symbol_errors.get(symbol)})
    return rows


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


# --- Décisions de Claude -------------------------------------------------------------------

@router.get("/ai/decisions")
def get_ai_decisions(services: Authed, limit: Annotated[int, Query(ge=1, le=500)] = 30) -> dict[str, Any]:
    return {"decisions": services.repo.recent_decisions(limit), "track_record": services.repo.ai_track_record()}


def _require_ai(services: Services) -> Any:
    if services.ai_trader is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Mode IA inactif : définir ANTHROPIC_API_KEY et DECISION_MODE=ai")
    return services.ai_trader


@router.post("/ai/analyze/{symbol}", status_code=status.HTTP_202_ACCEPTED)
async def analyze_now(symbol: str, services: Authed) -> dict[str, Any]:
    trader = _require_ai(services)
    if symbol not in services.settings.symbol_list:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Symbole inconnu : {symbol}")
    if not services.runner.is_running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Démarrez le bot avant de demander une analyse")
    has_position = services.stack.portfolio.position(symbol) is not None
    trader.spawn(trader.review(symbol, "manual") if has_position else trader.evaluate(symbol, "manual"))
    return {"status": "started", "kind": "review" if has_position else "entry"}


@router.post("/ai/briefing", status_code=status.HTTP_202_ACCEPTED)
async def refresh_briefing(services: Authed) -> dict[str, Any]:
    trader = _require_ai(services)
    trader.spawn(trader.refresh_briefing())
    return {"status": "started"}


# --- Optimiseur IA -------------------------------------------------------------------------

@router.get("/optimizer")
def get_optimizer(services: Authed) -> dict[str, Any]:
    return {"runs": services.repo.list_runs(10), "versions": services.repo.list_versions(20)}


class CapitalRequest(BaseModel):
    amount: float


@router.post("/account/capital")
async def set_capital(body: CapitalRequest, services: Authed) -> dict[str, Any]:
    """Modifie le capital de base (équivalent d'un dépôt ou d'un retrait sur le compte paper)."""
    if not 10 <= body.amount <= 1_000_000_000:
        raise HTTPException(422, "Montant entre 10 et 1 000 000 000")
    stack = services.stack
    try:
        delta = stack.portfolio.adjust_capital(body.amount)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    stack.risk.shift_baselines(delta)
    now = utcnow()
    services.repo.set_state("initial_capital", body.amount)
    services.repo.set_state("peak_reset_at", now.isoformat())
    services.bus.publish(EventType.EQUITY, to_payload(stack.portfolio.snapshot(now)))
    services.bus.publish(EventType.BOT, {"level": "INFO", "message": f"Capital de base modifié : "
                                         f"{body.amount:,.2f} (variation {delta:+,.2f})"})
    return build_status(services)


@router.get("/readiness")
def get_readiness(services: Authed) -> dict[str, Any]:
    return evaluate_readiness(services)


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
