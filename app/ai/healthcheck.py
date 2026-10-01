"""Vérification des agents IA avec la vraie clé API : ``python -m app check-ai``.

Chaque agent est appelé une fois sur des données réelles, sans passer aucun ordre :
décision (Opus), revue de position et leçon (Sonnet), actualités avec recherche web
(Sonnet), optimisation du scanner (Opus). Coût total d'environ 0,40 $, compté dans le
budget du jour.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import anthropic

from app.ai.advisor import ClaudeParameterAdvisor, OptimizationContext
from app.ai.briefing import BriefingService
from app.ai.claude import ClaudeClient
from app.ai.context import MarketContextBuilder
from app.ai.costs import CostTracker
from app.ai.decision import ClaudeTrader
from app.ai.optimizer import search_space
from app.config import Settings
from app.database.database import Database
from app.database.repository import TradingRepository
from app.engine.params import ParameterSet, risk_limits_from_settings


@dataclass(slots=True)
class AgentCheck:
    agent: str
    model: str
    ok: bool
    detail: str
    seconds: float = 0.0


def _timed(agent: str, model: str, call: Callable[[], str]) -> AgentCheck:
    start = time.monotonic()
    try:
        detail = call()
        return AgentCheck(agent, model, True, detail, time.monotonic() - start)
    except Exception as exc:  # noqa: BLE001 - on rapporte l'erreur de chaque agent sans s'arrêter
        return AgentCheck(agent, model, False, f"{type(exc).__name__} : {exc}"[:300], time.monotonic() - start)


def check_agents(settings: Settings, symbol: str | None = None) -> list[AgentCheck]:
    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        return [AgentCheck("Clé API", "-", False, "ANTHROPIC_API_KEY absente du fichier .env")]
    try:
        available = {m.id for m in anthropic.Anthropic(api_key=key).models.list(limit=100)}
    except anthropic.AuthenticationError:
        return [AgentCheck("Clé API", "-", False, "clé refusée par Anthropic (401) : remplacez-la dans .env")]
    results = [AgentCheck("Clé API", "-", True, f"{len(available)} modèles accessibles")]
    for model in {settings.ai_decision_model, settings.ai_review_model, settings.ai_briefing_model, settings.ai_model}:
        if model not in available:
            results.append(AgentCheck("Modèle", model, False, "modèle non disponible pour cette clé"))

    db = Database(settings.database_url)
    db.create_schema()
    repo = TradingRepository(db)
    costs = CostTracker(settings.ai_daily_budget_usd, settings.ai_max_calls_per_hour,
                        spent_today=repo.ai_spent_today(), sink=repo.insert_usage)
    claude = ClaudeClient(key, costs)
    from app.services import create_provider  # import local : évite un cycle services -> ai

    provider, _ = create_provider(settings)
    symbol = symbol or settings.symbol_list[0]
    trader = ClaudeTrader(claude, decision_model=settings.ai_decision_model,
                          decision_effort=settings.ai_decision_effort, review_model=settings.ai_review_model)
    builder = MarketContextBuilder(provider, timeframes=settings.context_timeframe_list)
    context: dict[str, Any] = {}

    def decision() -> str:
        context.update(builder.build(symbol, settings.timeframe))
        result, _ = trader.decide(context, review=False)
        return f"{symbol} : {result.action} (confiance {result.confidence:.2f}) — {result.thesis[:140]}"

    def review() -> str:
        bars = context["dernieres_bougies"]["valeurs"]
        price = float(bars[-1][4]) if bars else 0.0
        hypothetical = context | {"position_ouverte": {
            "note": "position fictive pour le test de l'agent, aucun ordre réel ou simulé",
            "sens": "LONG", "prix_entree": price, "stop_loss": round(price * 0.97, 6) if price else None,
            "take_profit": round(price * 1.06, 6) if price else None}}
        result, _ = trader.decide(hypothetical, review=True)
        return f"revue : {result.action} (confiance {result.confidence:.2f})"

    def lesson() -> str:
        report = {"trade": {"symbol": symbol, "direction": "LONG", "entry_price": 100.0, "exit_price": 97.0,
                            "net_pnl": -30.0, "r_multiple": -1.0, "reason": "Stop-loss touché",
                            "max_favorable_r": 0.4},
                  "decision_initiale": {"these": "cassure de résistance avec volume", "confiance": 0.7,
                                        "note": "trade fictif pour le test de l'agent"}}
        return "règle : " + trader.lesson(report).rule_for_next_time[:160]

    def briefing() -> str:
        service = BriefingService(claude, model=settings.ai_briefing_model, symbols=settings.symbol_list,
                                  interval_minutes=settings.ai_briefing_interval_minutes,
                                  max_searches=settings.ai_briefing_max_searches,
                                  blackout_minutes=settings.news_blackout_minutes, on_briefing=repo.save_briefing)
        result = asyncio.run(service.refresh())
        assert result is not None
        return (f"risque {result.risk_level}, sentiment {result.overall_sentiment:+.2f}, "
                f"{len(result.key_events)} événement(s) à l'agenda")

    def optimizer() -> str:
        active = repo.active_version()
        params = active[1].validated() if active else ParameterSet.from_settings(settings)
        advisor = ClaudeParameterAdvisor(key, settings.ai_model, settings.ai_effort, claude=claude)
        response = advisor.propose(OptimizationContext(
            strategy_name=params.strategy_name, timeframe=settings.timeframe,
            current_params=params.model_dump(mode="json"),
            search_space={k: {"min_max": [lo, hi]} for k, (lo, hi) in search_space(params.strategy_name).items()},
            risk_limits=asdict(risk_limits_from_settings(settings)), train_metrics={}, market_stats={},
            live_summary={"note": "test de l'agent : propositions non appliquées"}, candidates=1))
        return f"{len(response.proposals)} proposition(s) de réglage (non appliquées)"

    results.append(_timed("Décision de trading", settings.ai_decision_model, decision))
    if context:
        results.append(_timed("Revue de position", settings.ai_review_model, review))
    results.append(_timed("Leçon après trade", settings.ai_review_model, lesson))
    results.append(_timed("Actualités (recherche web)", settings.ai_briefing_model, briefing))
    results.append(_timed("Optimisation du scanner", settings.ai_model, optimizer))
    results.append(AgentCheck("Budget", "-", True, f"dépensé aujourd'hui : {costs.status()['spent_today_usd']:.2f} $ "
                                                   f"sur {settings.ai_daily_budget_usd:.2f} $"))
    db.dispose()
    return results
