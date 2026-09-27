"""Amélioration continue : Claude propose, le backtest walk-forward décide.

Procédure pour chaque cycle :
1. l'historique est découpé en période d'ENTRAÎNEMENT (vue par Claude) et période de
   VALIDATION (hors-échantillon, jamais montrée à Claude) ;
2. la configuration actuelle est backtestée sur les deux périodes (référence) ;
3. Claude propose N candidats dans l'espace de recherche ;
4. chaque candidat est validé (bornes), puis backtesté avec le MÊME moteur que le bot ;
5. un candidat n'est retenu que s'il améliore le score de validation d'au moins
   ``min_improvement``, ne dégrade pas l'entraînement, a assez de trades et respecte
   le drawdown maximal. Sinon, rien ne change.

Score = rendement total (%) - 0,5 x drawdown maximal (%), moyenné sur les symboles.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import ValidationError

from app.ai.advisor import OptimizationContext, ParameterAdvisor
from app.backtest.engine import run_backtest
from app.core.types import Candle
from app.engine.params import ParameterSet
from app.risk.risk_manager import ExitParams, RiskLimits
from app.strategy import STRATEGIES

logger = logging.getLogger(__name__)

Datasets = dict[str, list[Candle]]


@dataclass(frozen=True, slots=True)
class PeriodEvaluation:
    score: float
    trades: int
    total_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float | None
    per_symbol: dict[str, dict[str, Any]]


@dataclass(slots=True)
class CandidateEvaluation:
    params: dict[str, Any] | None
    rationale: str
    accepted: bool = False
    reason: str = ""
    train: PeriodEvaluation | None = None
    validation: PeriodEvaluation | None = None


@dataclass(slots=True)
class OptimizationResult:
    analysis: str
    baseline_train: PeriodEvaluation
    baseline_validation: PeriodEvaluation
    candidates: list[CandidateEvaluation] = field(default_factory=list)
    best: CandidateEvaluation | None = None

    @property
    def best_params(self) -> ParameterSet | None:
        return None if self.best is None else ParameterSet.model_validate(self.best.params)

    def summary(self) -> str:
        base = self.baseline_validation.score
        if self.best is None or self.best.validation is None:
            return f"Aucun candidat ne bat la configuration actuelle (score validation {base:.2f})."
        return (f"Candidat retenu : score validation {self.best.validation.score:.2f} "
                f"vs {base:.2f} actuellement.")

    def to_dict(self) -> dict[str, Any]:
        return {"analysis": self.analysis, "baseline_train": asdict(self.baseline_train),
                "baseline_validation": asdict(self.baseline_validation),
                "candidates": [asdict(c) for c in self.candidates]}


def split_datasets(datasets: Datasets, validation_bars: int) -> tuple[Datasets, Datasets]:
    """Coupe chaque série : [..., fin entraînement) et [fin entraînement, fin]."""
    train, validation = {}, {}
    for symbol, candles in datasets.items():
        cut = len(candles) - validation_bars
        if cut <= 0:
            raise ValueError(f"{symbol} : historique insuffisant ({len(candles)} bougies)")
        train[symbol], validation[symbol] = candles[:cut], candles
    return train, validation


def search_space(strategy_name: str) -> dict[str, tuple[float, float]]:
    return {**STRATEGIES[strategy_name].search_space, **ExitParams.search_space}


def check_search_space(params: ParameterSet) -> None:
    values = {**params.strategy, **params.exits.model_dump()}
    for name, (low, high) in search_space(params.strategy_name).items():
        value = values.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if not low <= float(value) <= high:
            raise ValueError(f"{name}={value} hors de l'espace de recherche [{low}, {high}]")


class StrategyOptimizer:
    def __init__(self, advisor: ParameterAdvisor, limits: RiskLimits, *, initial_capital: float,
                 timeframe: str, candidates: int = 3, min_trades: int = 8,
                 min_improvement: float = 0.5, lookback_bars: int = 300, currency: str = "USDT") -> None:
        self.advisor = advisor
        self.limits = limits
        self.initial_capital = initial_capital
        self.timeframe = timeframe
        self.candidates = candidates
        self.min_trades = min_trades
        self.min_improvement = min_improvement
        self.lookback_bars = lookback_bars
        self.currency = currency

    # -- Évaluation -------------------------------------------------------------------
    def evaluate(self, params: ParameterSet, datasets: Datasets, start_bars: dict[str, int]) -> PeriodEvaluation:
        """Backtest de chaque symbole à partir de l'index ``start_bars[symbol]`` (préchauffage inclus)."""
        warmup = params.build_strategy().warmup_bars
        per_symbol: dict[str, dict[str, Any]] = {}
        for symbol, candles in datasets.items():
            segment = candles[max(0, start_bars.get(symbol, 0) - warmup):]
            metrics = run_backtest(segment, params, self.limits, self.initial_capital,
                                   currency=self.currency, lookback_bars=self.lookback_bars).metrics
            per_symbol[symbol] = asdict(metrics) | {
                "score": metrics.total_return * 100 - 0.5 * metrics.max_drawdown * 100}
        return self._aggregate(per_symbol)

    @staticmethod
    def _aggregate(per_symbol: dict[str, dict[str, Any]]) -> PeriodEvaluation:
        n = len(per_symbol) or 1
        values = list(per_symbol.values())
        wins = sum(v["wins"] for v in values)
        trades = sum(v["trades"] for v in values)
        pfs = [v["profit_factor"] for v in values if v["profit_factor"] is not None]
        return PeriodEvaluation(
            score=sum(v["score"] for v in values) / n,
            trades=trades,
            total_return=sum(v["total_return"] for v in values) / n,
            max_drawdown=max((v["max_drawdown"] for v in values), default=0.0),
            win_rate=wins / trades if trades else 0.0,
            profit_factor=sum(pfs) / len(pfs) if pfs else None,
            per_symbol=per_symbol,
        )

    # -- Cycle complet ----------------------------------------------------------------
    def run(self, datasets: Datasets, validation_bars: int, current: ParameterSet,
            live_summary: dict[str, Any], history: Sequence[dict[str, Any]]) -> OptimizationResult:
        train, full = split_datasets(datasets, validation_bars)
        val_start = {s: len(train[s]) for s in train}
        baseline_train = self.evaluate(current, train, {})
        baseline_val = self.evaluate(current, full, val_start)
        context = OptimizationContext(
            strategy_name=current.strategy_name, timeframe=self.timeframe,
            current_params=current.model_dump(mode="json"),
            search_space={k: {"min_max": [lo, hi]} for k, (lo, hi) in search_space(current.strategy_name).items()},
            risk_limits=asdict(self.limits), train_metrics=asdict(baseline_train),
            market_stats={s: market_stats(c) for s, c in train.items()},
            live_summary=live_summary, history=list(history), candidates=self.candidates,
        )
        response = self.advisor.propose(context)
        result = OptimizationResult(response.analysis, baseline_train, baseline_val)
        for proposal in response.proposals[: self.candidates]:
            result.candidates.append(self._assess(proposal.strategy, proposal.exits, proposal.rationale,
                                                  current, train, full, val_start, result))
        accepted = [c for c in result.candidates if c.accepted and c.validation is not None]
        result.best = max(accepted, key=lambda c: c.validation.score, default=None)  # type: ignore[union-attr]
        logger.info("Optimisation : %s", result.summary())
        return result

    def _assess(self, strategy: dict[str, Any], exits: dict[str, Any], rationale: str,
                current: ParameterSet, train: Datasets, full: Datasets, val_start: dict[str, int],
                result: OptimizationResult) -> CandidateEvaluation:
        try:
            params = ParameterSet(strategy_name=current.strategy_name, strategy=strategy,
                                  exits=ExitParams(**exits)).validated()
            check_search_space(params)
        except (ValidationError, ValueError, TypeError) as exc:
            return CandidateEvaluation({"strategy": strategy, "exits": exits}, rationale,
                                       reason=f"Paramètres invalides : {exc}")
        candidate = CandidateEvaluation(params.model_dump(mode="json"), rationale)
        if params == current:
            candidate.reason = "Identique à la configuration actuelle"
            return candidate
        candidate.train = self.evaluate(params, train, {})
        candidate.validation = self.evaluate(params, full, val_start)
        candidate.accepted, candidate.reason = self._decide(candidate, result)
        return candidate

    def _decide(self, candidate: CandidateEvaluation, result: OptimizationResult) -> tuple[bool, str]:
        train, val = candidate.train, candidate.validation
        assert train is not None and val is not None
        if val.trades < self.min_trades:
            return False, f"Trop peu de trades en validation ({val.trades} < {self.min_trades})"
        if val.max_drawdown >= self.limits.max_drawdown:
            return False, f"Drawdown de validation {val.max_drawdown:.1%} trop élevé"
        if train.score < result.baseline_train.score:
            return False, "Dégrade la période d'entraînement"
        gain = val.score - result.baseline_validation.score
        if gain < self.min_improvement:
            return False, f"Amélioration de validation insuffisante ({gain:+.2f} < {self.min_improvement})"
        return True, f"Validation {val.score:.2f} vs {result.baseline_validation.score:.2f} ({gain:+.2f})"


def market_stats(candles: Sequence[Candle]) -> dict[str, float]:
    """Statistiques descriptives de la période d'entraînement (fournies à Claude)."""
    closes = [c.close for c in candles]
    returns = [b / a - 1 for a, b in zip(closes, closes[1:], strict=False)]
    mean = sum(returns) / len(returns) if returns else 0.0
    variance = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
    ranges = [(c.high - c.low) / c.close for c in candles]
    return {
        "bars": len(candles),
        "period_return": closes[-1] / closes[0] - 1 if closes else 0.0,
        "bar_volatility": math.sqrt(variance),
        "avg_bar_range_pct": sum(ranges) / len(ranges) if ranges else 0.0,
        "up_bars_share": sum(r > 0 for r in returns) / len(returns) if returns else 0.0,
    }
