"""Accès unique à l'API Claude : sorties structurées, recherche web, gestion d'erreurs et coûts.

Dépendance externe : API Anthropic (``ANTHROPIC_API_KEY``). Tous les appels passent par
ici pour être comptés dans le budget (``CostTracker``).
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from app.ai.costs import CostTracker

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
MAX_CONTINUATIONS = 4


class ClaudeError(RuntimeError):
    """Appel à Claude impossible ou réponse inexploitable."""


class ClaudeClient:
    def __init__(self, api_key: str, costs: CostTracker, *, client: Any = None, timeout: float = 240.0) -> None:
        self.costs = costs
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def structured(self, *, purpose: str, model: str, effort: str, system: str, user: str,
                   output_model: type[T], estimate_usd: float, max_tokens: int = 16_000) -> T:
        """Réponse validée contre ``output_model`` (sorties structurées de l'API)."""
        self.costs.check(estimate_usd, purpose)
        response = self._call(self._client.messages.parse, model=model, max_tokens=max_tokens,
                              system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                              messages=[{"role": "user", "content": user}],
                              output_config={"effort": effort}, output_format=output_model)
        self.costs.record(purpose, model, response.usage)
        self._check_stop(response)
        if response.parsed_output is None:
            raise ClaudeError("Réponse de Claude sans sortie structurée")
        return response.parsed_output

    def research(self, *, purpose: str, model: str, effort: str, system: str, user: str,
                 submit_tool: dict[str, Any], max_searches: int, estimate_usd: float) -> dict[str, Any]:
        """Recherche web puis appel de l'outil strict ``submit_tool`` ; retourne son entrée.

        Gère ``pause_turn`` (boucle serveur interrompue) et relance une fois Claude s'il
        termine sans appeler l'outil de soumission.
        """
        self.costs.check(estimate_usd, purpose)
        # Variante de base : le filtrage dynamique (20260209) consommait plusieurs utilisations par
        # recherche (max_uses_exceeded) et davantage de tokens lors de nos essais.
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_searches}, submit_tool]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        nudged = False
        for _ in range(MAX_CONTINUATIONS):
            response = self._call(self._client.messages.create, model=model, max_tokens=16_000,
                                  system=system, messages=messages, tools=tools,
                                  output_config={"effort": effort})
            self.costs.record(purpose, model, response.usage)
            self._check_stop(response, allow=("pause_turn", "tool_use", "end_turn"))
            _log_search_errors(response.content)
            submitted = next((b for b in response.content
                              if b.type == "tool_use" and b.name == submit_tool["name"]), None)
            if submitted is not None:
                return dict(submitted.input)
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": f"Appelle maintenant l'outil {submit_tool['name']} "
                                                        "avec ta synthèse."})
        raise ClaudeError(f"Claude n'a pas appelé {submit_tool['name']}")

    @staticmethod
    def _call(method: Any, **kwargs: Any) -> Any:
        try:
            return method(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ClaudeError("Clé API Anthropic invalide") from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeError("Limite de débit de l'API Anthropic atteinte") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeError(f"Erreur API Anthropic ({exc.status_code}) : {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeError("API Anthropic injoignable") from exc

    @staticmethod
    def _check_stop(response: Any, allow: tuple[str, ...] = ("end_turn",)) -> None:
        if response.stop_reason == "refusal":
            raise ClaudeError("Claude a refusé la requête (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise ClaudeError("Réponse de Claude tronquée (max_tokens)")
        if response.stop_reason not in allow:
            logger.warning("stop_reason inattendu : %s", response.stop_reason)


def _log_search_errors(content: Any) -> None:
    """Les erreurs de l'outil de recherche ne lèvent pas d'exception : on les journalise."""
    for block in content:
        if block.type == "web_search_tool_result" and not isinstance(block.content, list):
            logger.warning("Recherche web en erreur : %s", getattr(block.content, "error_code", block.content))
