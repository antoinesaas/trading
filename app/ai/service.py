"""Orchestration asynchrone des cycles d'optimisation (manuels ou planifiés)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.ai.optimizer import OptimizationResult, StrategyOptimizer
from app.core.events import EventBus, EventType
from app.database.repository import TradingRepository
from app.engine.params import ParameterSet
from app.engine.trading_engine import TradingEngine
from app.market.data_provider import MarketDataProvider

logger = logging.getLogger(__name__)


class OptimizerBusyError(RuntimeError):
    """Un cycle d'optimisation est déjà en cours."""


class OptimizerUnavailableError(RuntimeError):
    """Optimiseur non configuré (clé API absente ou désactivé)."""


class OptimizerService:
    def __init__(self, optimizer: StrategyOptimizer | None, engine: TradingEngine,
                 provider: MarketDataProvider, repository: TradingRepository, bus: EventBus, *,
                 symbols: list[str], timeframe: str, train_bars: int, validation_bars: int,
                 auto_apply: bool, interval_hours: float, scheduled: bool) -> None:
        self.optimizer = optimizer
        self.engine = engine
        self.provider = provider
        self.repo = repository
        self.bus = bus
        self.symbols = symbols
        self.timeframe = timeframe
        self.train_bars = train_bars
        self.validation_bars = validation_bars
        self.auto_apply = auto_apply
        self.interval_hours = interval_hours
        self.scheduled = scheduled and optimizer is not None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self.optimizer is not None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def model_name(self) -> str | None:
        return None if self.optimizer is None else self.optimizer.advisor.model_name

    async def run_once(self, trigger: str) -> dict[str, Any]:
        if self.optimizer is None:
            raise OptimizerUnavailableError("Optimiseur IA indisponible : définir ANTHROPIC_API_KEY.")
        if self._lock.locked():
            raise OptimizerBusyError("Une optimisation est déjà en cours.")
        async with self._lock:
            run_id = self.repo.start_run(trigger, self.optimizer.advisor.model_name)
            self._publish("INFO", f"Optimisation #{run_id} démarrée ({trigger})", run_id)
            try:
                current, live, history = self.engine.current_params(), self._live_summary(), self._history()
                result = await asyncio.to_thread(self._run_sync, current, live, history)
            except Exception as exc:  # noqa: BLE001 - l'échec d'un cycle ne doit jamais arrêter le bot
                logger.exception("Échec de l'optimisation #%d", run_id)
                self.repo.finish_run(run_id, status="error", error=str(exc))
                self._publish("ERROR", f"Optimisation #{run_id} en échec : {exc}", run_id)
                return {"run_id": run_id, "status": "error", "error": str(exc)}
            return self._conclude(run_id, result)

    def _run_sync(self, current: ParameterSet, live: dict[str, Any],
                  history: list[dict[str, Any]]) -> OptimizationResult:
        """Exécuté dans un thread : ne touche pas à l'état du moteur."""
        assert self.optimizer is not None
        bars = self.train_bars + self.validation_bars
        datasets = {s: self.provider.fetch_history(s, self.timeframe, bars) for s in self.symbols}
        return self.optimizer.run(datasets, self.validation_bars, current, live, history)

    def _conclude(self, run_id: int, result: OptimizationResult) -> dict[str, Any]:
        best = result.best_params
        status = "no_improvement"
        if best is not None and result.best is not None:
            metrics = {"validation": result.best.validation and result.best.validation.score,
                       "baseline_validation": result.baseline_validation.score}
            version_status = "active" if self.auto_apply else "proposed"
            self.repo.save_version(best, source="ai", status=version_status, metrics=metrics,
                                   rationale=result.best.rationale, run_id=run_id)
            if self.auto_apply:
                self.engine.apply_params(best)
            status = "applied" if self.auto_apply else "proposed"
        data = result.to_dict()
        self.repo.finish_run(run_id, status=status, summary=result.summary(), analysis=result.analysis,
                             baseline=data["baseline_validation"], candidates=data["candidates"])
        self._publish("INFO", f"Optimisation #{run_id} : {status} — {result.summary()}", run_id)
        return {"run_id": run_id, "status": status, "summary": result.summary()}

    def activate_version(self, version_id: int) -> ParameterSet:
        params = self.repo.activate_version(version_id)
        self.engine.apply_params(params)
        self._publish("INFO", f"Version de paramètres #{version_id} activée manuellement", None)
        return params

    async def schedule_loop(self) -> None:
        while self.scheduled:
            await asyncio.sleep(self.interval_hours * 3_600)
            try:
                await self.run_once("scheduled")
            except OptimizerBusyError:
                logger.info("Optimisation planifiée ignorée : un cycle est déjà en cours")

    def _live_summary(self) -> dict[str, Any]:
        trades = self.repo.recent_trades(limit=50)
        if not trades:
            return {"trades": 0}
        pnls = [t["net_pnl"] for t in trades]
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        return {"trades": len(trades), "net_pnl": sum(pnls),
                "win_rate": sum(p > 0 for p in pnls) / len(pnls),
                "avg_r": sum(t["r_multiple"] for t in trades) / len(trades), "exit_reasons": reasons}

    def _history(self) -> list[dict[str, Any]]:
        return [{"status": r["status"], "summary": r["summary"],
                 "candidates": [{"params": c.get("params"), "accepted": c.get("accepted"),
                                 "reason": c.get("reason")} for c in (r.get("candidates") or [])]}
                for r in self.repo.list_runs(limit=5) if r["status"] != "running"]

    def _publish(self, level: str, message: str, run_id: int | None) -> None:
        self.bus.publish(EventType.OPTIMIZATION, {"level": level, "message": message, "run_id": run_id})
