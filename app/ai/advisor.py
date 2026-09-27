"""Conseiller de paramètres basé sur Claude (API Anthropic, modèle ``claude-opus-5-5`` par défaut).

Claude ne passe AUCUN ordre et ne modifie rien directement : il propose des jeux de
paramètres, que ``StrategyOptimizer`` valide ensuite par backtest hors-échantillon.
Dépendance externe : API Anthropic (clé ``ANTHROPIC_API_KEY``). Remplaçable par toute
classe respectant le protocole ``ParameterAdvisor``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, create_model

from app.risk.risk_manager import ExitParams
from app.strategy import STRATEGIES, StrategyParams

logger = logging.getLogger(__name__)


class AdvisorError(RuntimeError):
    """Le conseiller n'a pas pu produire de proposition exploitable."""


@dataclass(frozen=True, slots=True)
class AdvisorProposal:
    strategy: dict[str, Any]
    exits: dict[str, Any]
    rationale: str


@dataclass(frozen=True, slots=True)
class AdvisorResponse:
    analysis: str
    proposals: list[AdvisorProposal]


@dataclass(frozen=True, slots=True)
class OptimizationContext:
    strategy_name: str
    timeframe: str
    current_params: dict[str, Any]
    search_space: dict[str, dict[str, list[float]]]
    risk_limits: dict[str, Any]
    train_metrics: dict[str, Any]
    market_stats: dict[str, Any]
    live_summary: dict[str, Any]
    history: list[dict[str, Any]] = field(default_factory=list)
    candidates: int = 3


class ParameterAdvisor(Protocol):
    model_name: str

    def propose(self, context: OptimizationContext) -> AdvisorResponse: ...


SYSTEM_PROMPT = """Tu es un chercheur quantitatif qui améliore en continu une stratégie de trading \
exécutée exclusivement en PAPER TRADING (aucun argent réel).

Ton rôle : analyser les statistiques fournies puis proposer des jeux de paramètres candidats.
Règles :
- Reste strictement dans l'espace de recherche fourni ; toute valeur hors bornes est rejetée.
- Les limites de risque du compte sont fixes : tu ne peux pas les modifier.
- Chaque candidat sera backtesté automatiquement sur une période de VALIDATION hors-échantillon \
que tu ne vois pas, et ne sera appliqué que s'il bat la configuration actuelle sans dépasser le \
drawdown autorisé. Les métriques fournies portent uniquement sur la période d'entraînement.
- Évite le sur-apprentissage : préfère des changements modérés et justifiés par les données \
(régime de volatilité, tendance, taux de réussite, sorties) plutôt que des valeurs extrêmes.
- Propose des candidats réellement différents les uns des autres.
- Tiens compte de l'historique des optimisations précédentes pour ne pas répéter des essais \
déjà rejetés.
Réponds en français, de façon concise."""


def build_output_model(strategy_params: type[StrategyParams]) -> type[BaseModel]:
    """Schéma de sortie structurée (sans contraintes numériques : les bornes sont vérifiées après)."""
    strategy_fields: dict[str, Any] = {
        name: (info.annotation, ...) for name, info in strategy_params.model_fields.items()}
    exit_fields: dict[str, Any] = {
        name: (info.annotation, ...) for name, info in ExitParams.model_fields.items()}
    strategy_model = create_model("StrategyProposal", **strategy_fields)
    exit_model = create_model("ExitProposal", **exit_fields)
    proposal_model = create_model("ParameterProposal", strategy=(strategy_model, ...),
                                  exits=(exit_model, ...), rationale=(str, ...))
    return create_model("OptimizationAdvice", analysis=(str, ...), proposals=(list[proposal_model], ...))


def render_context(context: OptimizationContext) -> str:
    sections = {
        "strategie": context.strategy_name,
        "timeframe": context.timeframe,
        "parametres_actuels": context.current_params,
        "espace_de_recherche": context.search_space,
        "limites_de_risque_fixes": context.risk_limits,
        "metriques_entrainement_parametres_actuels": context.train_metrics,
        "statistiques_de_marche_entrainement": context.market_stats,
        "resultats_paper_trading_recents": context.live_summary,
        "historique_optimisations": context.history,
    }
    return (
        "Voici l'état de la stratégie (JSON) :\n\n"
        + json.dumps(sections, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        + f"\n\nPropose exactement {context.candidates} jeux de paramètres candidats, chacun avec "
          "une justification courte, précédés d'une analyse synthétique de la situation."
    )


class ClaudeParameterAdvisor:
    def __init__(self, api_key: str, model: str = "claude-opus-5-5", effort: str = "high",
                 client: anthropic.Anthropic | None = None) -> None:
        self.model_name = model
        self.effort = effort
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def propose(self, context: OptimizationContext) -> AdvisorResponse:
        output_model = build_output_model(STRATEGIES[context.strategy_name].params_model)
        try:
            response = self._client.messages.parse(
                model=self.model_name,
                max_tokens=16_000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": render_context(context)}],
                output_config={"effort": self.effort},
                output_format=output_model,
            )
        except anthropic.AuthenticationError as exc:
            raise AdvisorError("Clé API Anthropic invalide") from exc
        except anthropic.RateLimitError as exc:
            raise AdvisorError("Limite de débit de l'API Anthropic atteinte, réessayer plus tard") from exc
        except anthropic.APIStatusError as exc:
            raise AdvisorError(f"Erreur API Anthropic ({exc.status_code}) : {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise AdvisorError("API Anthropic injoignable") from exc
        return self._to_response(response)

    @staticmethod
    def _to_response(response: Any) -> AdvisorResponse:
        if response.stop_reason == "refusal":
            raise AdvisorError("Claude a refusé la requête (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise AdvisorError("Réponse de Claude tronquée (max_tokens)")
        parsed = response.parsed_output
        if parsed is None:
            raise AdvisorError("Réponse de Claude sans sortie structurée exploitable")
        logger.info("Claude a proposé %d candidats (requête %s)", len(parsed.proposals),
                    getattr(response, "_request_id", "?"))
        return AdvisorResponse(
            analysis=parsed.analysis,
            proposals=[AdvisorProposal(p.strategy.model_dump(), p.exits.model_dump(), p.rationale)
                       for p in parsed.proposals],
        )
