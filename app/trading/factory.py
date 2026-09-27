"""Création du broker : seul le paper trading est autorisé."""

from __future__ import annotations

import logging

from app.core.events import EventBus
from app.risk.risk_manager import RiskLimits
from app.safety import ensure_paper_broker, reject_live_mode_request
from app.trading.orders import IdGenerator
from app.trading.paper_broker import PaperBroker
from app.trading.portfolio import Portfolio


def create_broker(kind: str, portfolio: Portfolio, bus: EventBus, limits: RiskLimits,
                  ids: IdGenerator | None = None, log: logging.Logger | None = None) -> PaperBroker:
    if kind != "paper":
        raise reject_live_mode_request(kind)
    broker = PaperBroker(portfolio, bus, limits, ids, log)
    ensure_paper_broker(broker)
    return broker
