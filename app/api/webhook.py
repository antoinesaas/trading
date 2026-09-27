"""Endpoint ``POST /webhook/tradingview``.

TradingView n'autorise pas d'en-têtes HTTP personnalisés sur ses webhooks : le secret
est donc transmis DANS le corps JSON et comparé en temps constant. Aucune donnée
reçue n'est considérée comme fiable : taille, JSON, secret, schéma, symbole, sens,
prix, ATR, horodatage, timeframe, doublons et écart au prix de marché sont vérifiés
avant que le signal n'atteigne le Risk Manager.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.types import Direction, Signal, SignalSource, ensure_utc, utcnow
from app.services import Services

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 4_096


class TradingViewAlert(BaseModel):
    """Charge utile attendue (voir ``pine/tradingview_strategy.pine``)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    secret: str = Field(min_length=1, max_length=256)
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,20}$")
    action: Literal["LONG", "SHORT"]
    price: float = Field(gt=0, allow_inf_nan=False)
    atr: float = Field(gt=0, allow_inf_nan=False)
    timestamp: datetime
    bar_time: datetime | None = None
    timeframe: str | None = Field(default=None, max_length=10)
    strategy: str = Field(default="tradingview", max_length=64, pattern=r"^[\w.\-]+$")

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> Any:
        return value.split(":")[-1].strip().upper() if isinstance(value, str) else value

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("timestamp", "bar_time")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)


def normalize_tv_interval(value: str) -> str:
    """Convertit ``{{interval}}`` TradingView (``15``, ``60``, ``240``, ``D``) en ``15m``, ``1h``, ``4h``, ``1d``."""
    raw = value.strip().upper()
    if raw in ("D", "1D"):
        return "1d"
    if raw.isdigit():
        minutes = int(raw)
        if minutes % 1_440 == 0:
            return f"{minutes // 1_440}d"
        if minutes % 60 == 0:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    return raw.lower()


class WebhookGuard:
    """Limitation de débit par IP et anti-rejeu (mémoire, fenêtre = âge max d'un signal)."""

    def __init__(self, rate_per_minute: int, dedupe_ttl_seconds: float) -> None:
        self._rate = rate_per_minute
        self._ttl = dedupe_ttl_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._seen: dict[str, float] = {}

    def allow(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[client]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self._rate:
            return False
        hits.append(now)
        return True

    def is_duplicate(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._ttl}
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


def _services(request: Request) -> Services:
    return request.app.state.services


def _guard(request: Request) -> WebhookGuard:
    return request.app.state.webhook_guard


def _refuse(services: Services, code: int, reason: str, data: dict[str, Any] | None = None) -> HTTPException:
    logger.warning("Webhook TradingView refusé (%d) : %s", code, reason)
    if data is not None:  # uniquement après authentification ; le secret n'est jamais stocké
        safe = {k: v for k, v in data.items() if k != "secret"}
        services.repo.insert_signal(safe | {"source": SignalSource.TRADINGVIEW.value,
                                            "direction": safe.get("action", "?")},
                                    status="invalid", reason=reason)
    return HTTPException(status_code=code, detail=reason)


async def _read_json(request: Request, services: Services) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise _refuse(services, 413, "Corps de requête trop volumineux")
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _refuse(services, status.HTTP_400_BAD_REQUEST, "JSON invalide") from None
    if not isinstance(data, dict):
        raise _refuse(services, status.HTTP_400_BAD_REQUEST, "Un objet JSON est attendu")
    return data


def _check_secret(services: Services, data: dict[str, Any], client: str) -> None:
    expected = services.settings.webhook_secret.get_secret_value().encode()
    provided = data.get("secret")
    if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), expected):
        logger.warning("Webhook TradingView : secret absent ou incorrect (IP %s)", client)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Secret invalide")


def _validate_alert(services: Services, alert: TradingViewAlert, data: dict[str, Any],
                    guard: WebhookGuard) -> None:
    s = services.settings
    now = utcnow()
    if alert.symbol not in s.symbol_list:
        raise _refuse(services, 422, f"Symbole {alert.symbol} non autorisé ({s.symbols})", data)
    if now - alert.timestamp > timedelta(seconds=s.webhook_max_signal_age_seconds):
        raise _refuse(services, 422, "Signal périmé", data)
    if alert.timestamp - now > timedelta(seconds=s.webhook_max_future_skew_seconds):
        raise _refuse(services, 422, "Horodatage dans le futur", data)
    if alert.timeframe and normalize_tv_interval(alert.timeframe) != s.timeframe:
        raise _refuse(services, 422, f"Timeframe {alert.timeframe} différent de TIMEFRAME={s.timeframe}", data)
    last = services.stack.engine.last_price(alert.symbol)
    if last and abs(alert.price - last) / last > s.webhook_max_price_deviation:
        raise _refuse(services, 422, f"Prix {alert.price} trop éloigné du marché ({last})", data)
    if not math.isfinite(alert.price / alert.atr):
        raise _refuse(services, 422, "Rapport prix/ATR invalide", data)
    key = f"{alert.symbol}|{alert.action}|{(alert.bar_time or alert.timestamp).isoformat()}"
    if guard.is_duplicate(key):
        raise _refuse(services, 409, "Signal déjà reçu (doublon)", data)


@router.post("/webhook/tradingview", status_code=status.HTTP_202_ACCEPTED)
async def tradingview_webhook(request: Request) -> dict[str, Any]:
    services, guard = _services(request), _guard(request)
    settings = services.settings
    client = request.client.host if request.client else "unknown"
    if not settings.webhook_secret_is_secure:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Webhook désactivé : définir un WEBHOOK_SECRET d'au moins 16 caractères")
    if settings.ip_allowlist and client not in settings.ip_allowlist:
        logger.warning("Webhook TradingView : IP %s hors liste blanche", client)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IP non autorisée")
    if not guard.allow(client):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop de requêtes")

    data = await _read_json(request, services)
    _check_secret(services, data, client)
    try:
        alert = TradingViewAlert.model_validate(data)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False)]
        raise _refuse(services, 422, "Payload invalide — " + "; ".join(errors), data) from None
    if settings.signal_source == "internal":
        raise _refuse(services, 409, "Signaux TradingView désactivés (SIGNAL_SOURCE=internal)", data)
    _validate_alert(services, alert, data, guard)
    if not services.runner.is_running:
        raise _refuse(services, 409, f"Bot non démarré (état {services.runner.status})", data)

    signal = Signal(symbol=alert.symbol, direction=Direction(alert.action), price=alert.price,
                    atr=alert.atr, timestamp=alert.timestamp, strategy=alert.strategy,
                    reason=f"Alerte TradingView {alert.action} ({alert.strategy})",
                    source=SignalSource.TRADINGVIEW)
    if services.ai_trader is not None:  # Claude décide ; réponse immédiate (délai TradingView court)
        services.ai_trader.spawn(services.ai_trader.evaluate(alert.symbol, "tradingview", signal))
        return {"status": "queued", "reason": "Signal transmis à Claude pour décision", "order_id": None}
    outcome = services.stack.engine.handle_signal(signal, utcnow())
    return {"status": outcome.status, "reason": outcome.reason, "order_id": outcome.order_id}
