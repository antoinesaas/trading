"""Racine de composition : construit et relie tous les composants à partir de ``Settings``."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from app.ai.advisor import ClaudeParameterAdvisor, ParameterAdvisor
from app.ai.optimizer import StrategyOptimizer
from app.ai.service import OptimizerService
from app.api.realtime import Broadcaster
from app.bot.runner import BotRunner
from app.config import Settings
from app.core.events import EventBus
from app.database.database import Database
from app.database.repository import DatabaseRecorder, TradingRepository
from app.engine.params import ParameterSet, risk_limits_from_settings
from app.engine.stack import TradingStack, build_trading_stack
from app.market.data_provider import BinanceMarketDataProvider, CSVMarketDataProvider, MarketDataProvider
from app.safety import assert_no_live_credentials, assert_paper_only
from app.trading.orders import IdGenerator
from app.trading.portfolio import Portfolio

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    repo: TradingRepository
    bus: EventBus
    stack: TradingStack
    provider: MarketDataProvider
    runner: BotRunner
    optimizer: OptimizerService
    broadcaster: Broadcaster


def create_provider(settings: Settings) -> MarketDataProvider:
    if settings.market_data_provider == "csv":
        return CSVMarketDataProvider(settings.csv_data_dir)
    return BinanceMarketDataProvider(settings.binance_base_url)


def create_advisor(settings: Settings) -> ParameterAdvisor | None:
    if not settings.ai_available:
        return None
    return ClaudeParameterAdvisor(settings.anthropic_api_key.get_secret_value(), settings.ai_model,
                                  settings.ai_effort)


def _active_params(repo: TradingRepository, settings: Settings) -> ParameterSet:
    active = repo.active_version()
    if active is not None:
        logger.info("Paramètres actifs : version #%d (base de données)", active[0])
        return active[1].validated()
    params = ParameterSet.from_settings(settings)
    version_id = repo.save_version(params, source="config", status="active",
                                   rationale="Configuration initiale (.env)")
    logger.info("Paramètres initiaux enregistrés comme version #%d", version_id)
    return params


def build_services(settings: Settings, *, provider: MarketDataProvider | None = None,
                   advisor: ParameterAdvisor | None = None, env_file: Path | None = Path(".env")) -> Services:
    assert_paper_only(settings)
    env_keys = dotenv_values(env_file).keys() if env_file is not None and env_file.exists() else ()
    assert_no_live_credentials(os.environ, env_keys)

    db = Database(settings.database_url)
    db.create_schema()
    repo = TradingRepository(db)
    state = repo.load_account_state(settings.initial_capital)
    portfolio = Portfolio(state.initial_capital, state.balance, state.peak_equity)
    portfolio.positions = {p.symbol: p for p in state.positions}
    portfolio.trades = repo.load_trades()

    bus = EventBus()
    bus.subscribe(DatabaseRecorder(repo))
    broadcaster = Broadcaster()
    bus.subscribe(broadcaster)

    stack = build_trading_stack(
        _active_params(repo, settings), risk_limits_from_settings(settings), state.initial_capital,
        bus=bus, portfolio=portfolio, ids=IdGenerator(state.id_counters),
        currency=settings.account_currency, lookback_bars=settings.lookback_bars,
        internal_signals=settings.signal_source in ("internal", "both"),
    )
    stack.risk.restore(state.day, state.day_start_equity, state.halted, state.halt_reason)
    provider = provider or create_provider(settings)
    runner = BotRunner(stack.engine, provider, bus, symbols=settings.symbol_list,
                       timeframe=settings.timeframe, poll_interval=settings.poll_interval_seconds,
                       repository=repo, last_processed=state.last_processed)

    advisor = advisor or create_advisor(settings)
    optimizer = StrategyOptimizer(
        advisor, stack.risk.limits, initial_capital=state.initial_capital, timeframe=settings.timeframe,
        candidates=settings.ai_candidates, min_trades=settings.ai_min_trades,
        min_improvement=settings.ai_min_improvement, lookback_bars=settings.lookback_bars,
        currency=settings.account_currency,
    ) if advisor is not None else None
    optimizer_service = OptimizerService(
        optimizer, stack.engine, provider, repo, bus, symbols=settings.symbol_list,
        timeframe=settings.timeframe, train_bars=settings.ai_train_bars,
        validation_bars=settings.ai_validation_bars, auto_apply=settings.ai_auto_apply,
        interval_hours=settings.ai_optimizer_interval_hours, scheduled=settings.ai_optimizer_enabled,
    )
    if state.positions:
        logger.info("%d position(s) ouverte(s) restaurée(s) depuis la base", len(state.positions))
    return Services(settings, db, repo, bus, stack, provider, runner, optimizer_service, broadcaster)
