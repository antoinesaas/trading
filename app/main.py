"""Application FastAPI : dashboard, API de contrôle, WebSocket et webhook TradingView.

Lancement : ``python -m app serve`` (ou ``uvicorn app.main:create_app --factory``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.dashboard import router as api_router
from app.api.dashboard import ws_router
from app.api.webhook import WebhookGuard
from app.api.webhook import router as webhook_router
from app.config import Settings, get_settings
from app.core.events import EventType
from app.logging_setup import configure_logging
from app.safety import print_startup_warning
from app.services import Services, build_services

logger = logging.getLogger(__name__)
DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


def _security_warnings(services: Services) -> None:
    settings = services.settings
    if not settings.webhook_secret_is_secure:
        logger.warning("WEBHOOK_SECRET absent ou trop faible : le webhook TradingView est DÉSACTIVÉ.")
    if not settings.dashboard_token_is_secure:
        logger.warning("DASHBOARD_TOKEN absent ou trop faible : générez-en un avec `python -m app init-env`.")
    if not services.optimizer.available:
        logger.info("Optimiseur IA inactif (ANTHROPIC_API_KEY non définie).")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    services: Services = app.state.services
    print_startup_warning()
    _security_warnings(services)
    services.bus.publish(EventType.BOT, {"level": "INFO", "message": "Serveur démarré — PAPER TRADING UNIQUEMENT"})
    tasks: list[asyncio.Task[None]] = []
    if services.optimizer.scheduled:
        tasks.append(asyncio.create_task(services.optimizer.schedule_loop(), name="optimizer"))
    if services.settings.bot_auto_start:
        await services.runner.start()
    try:
        yield
    finally:
        await services.runner.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        services.db.dispose()


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    if services is None:
        settings = settings or get_settings()
        configure_logging(settings.log_level, settings.log_dir)
        services = build_services(settings)
    app = FastAPI(title="Paper Trading Bot", version=__version__, lifespan=lifespan,
                  description="Bot de trading algorithmique — PAPER TRADING UNIQUEMENT")
    app.state.services = services
    app.state.webhook_guard = WebhookGuard(
        services.settings.webhook_rate_limit_per_minute,
        services.settings.webhook_max_signal_age_seconds + services.settings.webhook_max_future_skew_seconds,
    )
    app.include_router(webhook_router)
    app.include_router(api_router)
    app.include_router(ws_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, object]:
        return {"status": "ok", "mode": "paper", "paper_only": True, "bot": services.runner.status.value}

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard/")

    if DASHBOARD_DIR.exists():
        app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")
    return app
