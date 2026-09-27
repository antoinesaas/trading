"""Orchestration des décisions de Claude en temps réel.

Déclencheurs :
- ``setup``       : clôture d'une bougie avec un signal ou un score de scanner suffisant ;
- ``tradingview`` : alerte reçue par webhook ;
- ``manual``      : bouton « Analyser maintenant » du dashboard ;
- ``review``      : revue périodique d'une position ouverte.

Avant tout appel payant, les blocages gratuits sont vérifiés (bot en pause, limites de
risque, positions max, heures de marché, blackout d'annonce). Les appels à Claude
tournent dans un thread ; la décision est appliquée dans la boucle d'événements, via le
moteur, donc sous le contrôle du ``RiskManager``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.ai.briefing import BriefingService
from app.ai.claude import ClaudeError
from app.ai.context import MarketContextBuilder
from app.ai.costs import BudgetExceededError
from app.ai.decision import ClaudeTrader, TradeDecision
from app.core.events import EventBus, EventType
from app.core.types import Direction, OrderIntent, OrderType, Signal, SignalSource, utcnow
from app.database.repository import TradingRepository
from app.engine.trading_engine import TradePlan, TradingEngine
from app.market.sessions import entry_window_block, market_clock
from app.market.timeframes import timeframe_seconds

logger = logging.getLogger(__name__)
MAX_TIME_STOP_HOURS = 24 * 7


@dataclass(frozen=True, slots=True)
class AITraderConfig:
    symbols: list[str]
    timeframe: str
    min_confidence: float = 0.65
    min_scan_score: float = 50.0
    reevaluate_hours: float = 8.0
    review_minutes: int = 120
    trade_weekends: bool = True
    no_trade_hours: frozenset[int] = frozenset()
    max_spread_pct: float = 0.002
    max_concurrent: int = 2


class AITrader:
    def __init__(self, engine: TradingEngine, trader: ClaudeTrader, context: MarketContextBuilder,
                 repo: TradingRepository, bus: EventBus, config: AITraderConfig,
                 briefing: BriefingService | None = None) -> None:
        self.engine = engine
        self.trader = trader
        self.context = context
        self.repo = repo
        self.bus = bus
        self.config = config
        self.briefing = briefing
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._busy: set[str] = set()
        self._last_eval: dict[str, datetime] = {}
        self._last_review: dict[str, datetime] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- Déclencheurs ---------------------------------------------------------------
    async def after_tick(self, fresh_symbols: list[str]) -> None:
        """Appelé par la boucle du bot après chaque cycle (non bloquant)."""
        now = utcnow()
        hints = {s.symbol: s for s in self.engine.drain_candidates()}
        for symbol in fresh_symbols:
            if self.engine.portfolio.position(symbol) is None and self._setup_worth_asking(symbol, hints, now):
                self.spawn(self.evaluate(symbol, "setup", hints.get(symbol)))
        for symbol in list(self.engine.portfolio.positions):
            last = self._last_review.get(symbol)
            if last is None or now - last >= timedelta(minutes=self.config.review_minutes):
                self._last_review[symbol] = now
                self.spawn(self.review(symbol))
        self._expire_limit_orders(now)
        if self.briefing is not None and self.briefing.due(now):
            self.spawn(self.refresh_briefing())

    def spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _setup_worth_asking(self, symbol: str, hints: dict[str, Signal], now: datetime) -> bool:
        if symbol in hints:
            return True
        analysis = self.engine.last_analysis(symbol)
        scores = [v for k, v in (analysis.indicators if analysis else {}).items() if k.startswith("score_")]
        if not scores or max(scores) < self.config.min_scan_score:
            return False
        last = self._last_eval.get(symbol)
        return last is None or now - last >= timedelta(hours=self.config.reevaluate_hours)

    async def _ensure_briefing(self) -> None:
        if self.briefing is not None:
            try:
                await self.briefing.ensure_ready()
            except (ClaudeError, BudgetExceededError) as exc:
                logger.warning("Décision sans briefing à jour : %s", exc)

    async def refresh_briefing(self) -> None:
        try:
            await self.briefing.refresh()  # type: ignore[union-attr]
        except (ClaudeError, BudgetExceededError) as exc:
            logger.warning("Briefing de marché non rafraîchi : %s", exc)

    # -- Décision d'entrée -------------------------------------------------------------
    async def evaluate(self, symbol: str, trigger: str, hint: Signal | None = None) -> dict[str, Any]:
        if symbol in self._busy:
            return {"status": "skipped", "reason": "Analyse déjà en cours"}
        self._busy.add(symbol)
        try:
            now = utcnow()
            self._last_eval[symbol] = now
            blocked = self._entry_block(symbol, now)
            if blocked:
                return self._record(symbol, trigger, "skipped", blocked)
            async with self._semaphore:
                return await self._decide_entry(symbol, trigger, hint)
        finally:
            self._busy.discard(symbol)

    def _entry_block(self, symbol: str, now: datetime) -> str | None:
        engine = self.engine
        if not engine.entries_enabled:
            return "Bot en pause ou arrêté"
        account = engine.account(now)
        blocked = engine.risk.entries_blocked_reason(account)
        if blocked:
            return blocked
        if account.open_positions >= engine.risk.limits.max_open_positions:
            return "Nombre maximal de positions atteint"
        if engine.portfolio.position(symbol) is not None:
            return "Position déjà ouverte sur ce symbole"
        window = entry_window_block(symbol, now, trade_weekends=self.config.trade_weekends,
                                    no_trade_hours_utc=self.config.no_trade_hours)
        if window:
            return window
        blackout = self.briefing.blackout(now) if self.briefing else None
        return f"Blackout d'annonce : {blackout.reason}" if blackout else None

    async def _decide_entry(self, symbol: str, trigger: str, hint: Signal | None) -> dict[str, Any]:
        await self._ensure_briefing()
        view = self._portfolio_view(symbol)
        try:
            context = await asyncio.to_thread(self._full_context, symbol, trigger, hint, view)
            decision, model = await asyncio.to_thread(self.trader.decide, context, review=False)
        except (ClaudeError, BudgetExceededError) as exc:
            return self._record(symbol, trigger, "error" if isinstance(exc, ClaudeError) else "skipped", str(exc))
        decision_id = self._save(symbol, trigger, model, decision, context)
        status, reason, order_id = self._execute_entry(symbol, decision, decision_id, context)
        self.repo.update_decision(decision_id, status=status, status_reason=reason, order_id=order_id)
        return self._publish(decision_id, symbol, trigger, decision, status, reason)

    def _execute_entry(self, symbol: str, decision: TradeDecision, decision_id: int,
                       context: dict[str, Any]) -> tuple[str, str, str | None]:
        if decision.action == "HOLD":
            return "hold", "Claude préfère ne pas entrer", None
        if decision.action not in ("OPEN_LONG", "OPEN_SHORT") or decision.entry is None:
            return "ignored", f"Action {decision.action} sans position ouverte", None
        if decision.confidence < self.config.min_confidence:
            return "rejected", f"Confiance {decision.confidence:.2f} < {self.config.min_confidence:.2f}", None
        spread = (context.get("marche", {}).get("carnet_ordres") or {}).get("spread_pct")
        if spread is not None and spread > self.config.max_spread_pct:
            return "rejected", f"Spread {spread:.4%} trop large", None
        price = self.engine.last_price(symbol)
        atr_value = _decision_atr(context, self.config.timeframe)
        if not price or not atr_value:
            return "rejected", "Prix ou ATR indisponible", None
        entry = decision.entry
        direction = Direction.LONG if decision.action == "OPEN_LONG" else Direction.SHORT
        order_type = OrderType.LIMIT if entry.order_type == "LIMIT" and entry.limit_price else OrderType.MARKET
        if order_type is OrderType.LIMIT and not (0 < direction.sign * (price - entry.limit_price) <= 2 * atr_value):
            order_type = OrderType.MARKET  # limite incohérente ou trop lointaine : exécution au marché
        reference = entry.limit_price if order_type is OrderType.LIMIT else price
        plan = TradePlan(
            stop_loss=entry.stop_loss, take_profit=entry.take_profit,
            requested_risk=max(0.0, entry.risk_percent) / 100, confidence=decision.confidence,
            tp1_price=entry.partial_take_profit_price,
            tp1_fraction=min(0.9, max(0.1, entry.partial_take_profit_fraction or 0.5)),
            breakeven_at_r=max(0.0, entry.breakeven_at_r or 0.0),
            time_stop=timedelta(hours=min(MAX_TIME_STOP_HOURS, entry.time_stop_hours))
            if entry.time_stop_hours and entry.time_stop_hours > 0 else None,
            order_type=order_type, limit_price=entry.limit_price if order_type is OrderType.LIMIT else None,
            decision_id=decision_id,
        )
        signal = Signal(symbol=symbol, direction=direction, price=reference, atr=atr_value, timestamp=utcnow(),
                        strategy="claude", reason=f"Claude ({decision.confidence:.2f}) : {decision.thesis[:200]}",
                        source=SignalSource.INTERNAL)
        outcome = self.engine.open_trade(signal, plan, utcnow())
        status = "executed" if outcome.status == "accepted" else outcome.status
        return status, outcome.reason, outcome.order_id

    # -- Revue de position ---------------------------------------------------------------
    async def review(self, symbol: str, trigger: str = "review") -> dict[str, Any]:
        if symbol in self._busy or self.engine.portfolio.position(symbol) is None:
            return {"status": "skipped", "reason": "Pas de position ou analyse en cours"}
        self._busy.add(symbol)
        try:
            async with self._semaphore:
                await self._ensure_briefing()
                view = self._portfolio_view(symbol)
                try:
                    context = await asyncio.to_thread(self._full_context, symbol, trigger, None, view)
                    decision, model = await asyncio.to_thread(self.trader.decide, context, review=True)
                except (ClaudeError, BudgetExceededError) as exc:
                    return self._record(symbol, trigger, "error", str(exc))
                decision_id = self._save(symbol, trigger, model, decision, context)
                status, reason = self._apply_review(symbol, decision)
                self.repo.update_decision(decision_id, status=status, status_reason=reason)
                return self._publish(decision_id, symbol, trigger, decision, status, reason)
        finally:
            self._busy.discard(symbol)

    def _apply_review(self, symbol: str, decision: TradeDecision) -> tuple[str, str]:
        now = utcnow()
        if decision.action == "CLOSE":
            return "executed", self.engine.manage_position(symbol, now, close=True,
                                                           reason=f"Clôture IA : {decision.thesis[:120]}")
        if decision.action == "ADJUST" and decision.adjustment is not None:
            result = self.engine.manage_position(
                symbol, now, new_stop=decision.adjustment.new_stop_loss,
                new_target=decision.adjustment.new_take_profit, reason=decision.thesis[:120])
            return ("executed" if "->" in result else "rejected"), result
        return "hold", "Position conservée"

    # -- Contexte -------------------------------------------------------------------------
    def _portfolio_view(self, symbol: str) -> dict[str, Any]:
        """Photographie du portefeuille, lue dans la boucle d'événements."""
        engine, now = self.engine, utcnow()
        account = engine.account(now)
        lim = engine.risk.limits
        positions = []
        for p in engine.portfolio.positions.values():
            positions.append({
                "symbole": p.symbol, "sens": p.direction.value, "quantite": p.quantity,
                "prix_entree": p.entry_price, "stop": p.stop_loss, "objectif": p.take_profit,
                "objectif_partiel": p.tp1_price, "objectif_partiel_pris": p.tp1_done,
                "point_mort_active": p.breakeven_done, "r_actuel": round(p.r_multiple(), 2),
                "pnl_latent": round(p.unrealized_pnl(), 2), "ouverte_depuis": p.entry_time.isoformat(),
                "meilleur_r_atteint": round(p.r_multiple(p.best_price), 2), "decision_id": p.decision_id,
            })
        return {
            "prix_actuel": engine.last_price(symbol),
            "capital": {"equity": round(account.equity, 2), "balance": round(engine.portfolio.balance, 2),
                        "cash_disponible": round(account.available_cash, 2),
                        "drawdown_pct": round(account.drawdown * 100, 2),
                        "perte_du_jour_pct": round(engine.risk.daily_loss() * 100, 2),
                        "perte_semaine_pct": round(engine.risk.weekly_loss() * 100, 2)},
            "positions_ouvertes": positions,
            "risque_ouvert_pct": round(account.open_risk.total * 100, 2),
            "statistiques": {"trades": account.stats.trades, "taux_reussite": round(account.stats.win_rate, 3),
                             "ratio_gain_perte": round(account.stats.payoff_ratio, 2),
                             "pertes_consecutives": account.stats.consecutive_losses},
            "limites_non_negociables": {
                "risque_max_par_trade_pct": lim.hard_max_risk_per_trade * 100,
                "risque_total_max_pct": lim.max_portfolio_risk * 100,
                "risque_meme_sens_max_pct": lim.max_correlated_risk * 100,
                "rendement_risque_min": lim.min_reward_risk,
                "stop_en_atr": [lim.min_stop_atr, lim.max_stop_atr],
                "positions_max": lim.max_open_positions, "confiance_min": self.config.min_confidence,
                "frais_par_cote_pct": lim.fee_rate * 100},
        }

    def _full_context(self, symbol: str, trigger: str, hint: Signal | None,
                      view: dict[str, Any]) -> dict[str, Any]:
        """Contexte complet (réseau et base de données : exécuté dans un thread)."""
        now = utcnow()
        briefing = self.briefing.latest if self.briefing else None
        context: dict[str, Any] = {
            "declencheur": trigger,
            "heure_utc": now.isoformat(),
            "sessions_de_marche": market_clock(now).as_dict(),
            "marche": self.context.build(symbol, self.config.timeframe),
            "portefeuille": view,
            "actualites": briefing.for_prompt() if briefing else "briefing indisponible",
            "ton_historique": self.repo.ai_track_record(),
        }
        if hint is not None:
            context["signal_du_scanner"] = {"sens": hint.direction.value, "prix": hint.price,
                                            "raison": hint.reason, "source": hint.source.value,
                                            "indicateurs": hint.indicators}
        position = self.engine.portfolio.position(symbol)
        if position is not None and position.decision_id:
            original = next((d for d in self.repo.recent_decisions(200) if d["id"] == position.decision_id), None)
            if original:
                context["these_initiale"] = {"these": original["thesis"], "invalidation": original["invalidation"]}
        return context

    # -- Persistance ---------------------------------------------------------------------
    def _save(self, symbol: str, trigger: str, model: str, decision: TradeDecision,
              context: dict[str, Any]) -> int:
        return self.repo.save_decision(
            symbol=symbol, trigger=trigger, model=model, action=decision.action, confidence=decision.confidence,
            status="pending", market_regime=decision.market_regime, thesis=decision.thesis,
            invalidation=decision.invalidation,
            details=decision.model_dump(mode="json", include={"key_factors", "risks", "entry", "adjustment"}),
            context=context)

    def _record(self, symbol: str, trigger: str, status: str, reason: str) -> dict[str, Any]:
        logger.info("IA %s %s : %s — %s", trigger, symbol, status, reason)
        if trigger != "setup" or status == "error":
            self.repo.save_decision(symbol=symbol, trigger=trigger, model="-", action="-", status=status,
                                    status_reason=reason)
        self.bus.publish(EventType.AI_DECISION, {"symbol": symbol, "trigger": trigger, "status": status,
                                                 "reason": reason})
        return {"status": status, "reason": reason}

    def _publish(self, decision_id: int, symbol: str, trigger: str, decision: TradeDecision,
                 status: str, reason: str) -> dict[str, Any]:
        logger.info("IA %s %s : %s (confiance %.2f) -> %s — %s", trigger, symbol, decision.action,
                    decision.confidence, status, reason)
        payload = {"id": decision_id, "symbol": symbol, "trigger": trigger, "action": decision.action,
                   "confidence": decision.confidence, "status": status, "reason": reason,
                   "thesis": decision.thesis}
        self.bus.publish(EventType.AI_DECISION, payload)
        return payload

    def _expire_limit_orders(self, now: datetime) -> None:
        horizon = timedelta(seconds=timeframe_seconds(self.config.timeframe))
        for order in self.engine.broker.pending_orders():
            if (order.intent is OrderIntent.OPEN and order.order_type is OrderType.LIMIT
                    and now - order.created_at > horizon):
                self.engine.broker.cancel_order(order.id, "Ordre limite expiré (une bougie)")


def _decision_atr(context: dict[str, Any], timeframe: str) -> float | None:
    frame = context.get("marche", {}).get("analyse_multi_timeframe", {}).get(timeframe, {})
    value = frame.get("atr14") if isinstance(frame, dict) else None
    return float(value) if value else None
