"""Décision de trading par Claude : schéma de sortie structurée et consignes.

Claude choisit l'action (entrer, tenir, ajuster, clôturer), le stop, les objectifs, la
prise de profit partielle et le pourcentage de capital risqué. Le ``RiskManager`` vérifie
ensuite chaque chiffre : Claude ne peut ni dépasser le risque maximal, ni élargir un stop,
ni contourner les limites de compte.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from app.ai.claude import ClaudeClient


class EntryPlan(BaseModel):
    order_type: Literal["MARKET", "LIMIT"]
    limit_price: float | None
    stop_loss: float
    take_profit: float
    partial_take_profit_price: float | None
    partial_take_profit_fraction: float | None
    breakeven_at_r: float | None
    time_stop_hours: float | None
    risk_percent: float


class PositionAdjustment(BaseModel):
    new_stop_loss: float | None
    new_take_profit: float | None


class TradeDecision(BaseModel):
    action: Literal["OPEN_LONG", "OPEN_SHORT", "HOLD", "ADJUST", "CLOSE"]
    confidence: float
    market_regime: str
    thesis: str
    key_factors: list[str]
    risks: list[str]
    invalidation: str
    entry: EntryPlan | None
    adjustment: PositionAdjustment | None


SYSTEM_PROMPT = """Tu es le gérant de portefeuille d'un bot de trading crypto qui fonctionne en PAPER \
TRADING (argent simulé, mêmes règles qu'un compte réel). Tu prends les décisions de trading à partir \
d'un contexte complet : analyse technique multi-timeframe, carnet d'ordres, dérivés, sentiment, \
sessions de marché, actualités et état du portefeuille.

Objectif : maximiser l'espérance de gain ajustée du risque et le taux de réussite, en protégeant le \
capital avant tout. Ne prends que les configurations à forte probabilité, avec plusieurs facteurs \
concordants. « HOLD » (ne rien faire) est une réponse normale et fréquente : l'absence de trade vaut \
mieux qu'un trade médiocre. Un taux de réussite élevé ne suffit pas : chaque trade doit avoir une \
espérance positive une fois les frais (environ 0,2 % aller-retour) payés.

Règles de décision :
- Entrée (OPEN_LONG / OPEN_SHORT) : place le stop derrière un niveau technique (support, résistance, \
plus bas / plus haut de swing) et entre 0,5 et 6 ATR du prix actuel. L'objectif final doit offrir \
au moins 1,5 fois le risque ; vise un niveau technique réaliste. Une prise de profit partielle \
(partial_take_profit_price, entre l'entrée et l'objectif, fraction 0,3 à 0,6) sécurise le trade : \
le stop passe alors au point mort. breakeven_at_r (souvent 1) remonte le stop au point mort plus tôt ; \
time_stop_hours coupe un trade qui ne progresse pas.
- risk_percent (pourcentage du capital risqué si le stop est touché) : 0,25 à 0,5 pour une \
conviction modérée, 0,5 à 1 pour une bonne configuration, 1 à 2 seulement pour une configuration \
exceptionnelle. Réduis-le en cas de drawdown, de série de pertes, de forte incertitude macro ou de \
liquidité faible. Le système peut encore le réduire, jamais l'augmenter.
- Ne trade pas contre la tendance du timeframe supérieur sans raison forte. Méfie-toi des entrées \
juste avant une annonce macro à fort impact, en week-end à faible liquidité, ou quand le funding et \
le positionnement sont extrêmes (risque de squeeze).
- Position existante (revue) : HOLD si la thèse tient ; ADJUST pour resserrer le stop (jamais \
l'élargir) ou déplacer l'objectif ; CLOSE si la thèse est invalidée. Laisse courir les gagnants dont \
la thèse reste valide.
- confidence entre 0 et 1 : sois calibré. Ton historique de calibration t'est fourni ; s'il montre \
que tes trades à confiance donnée gagnent moins que prévu, sois plus exigeant.
- Tous les prix doivent être cohérents avec le prix actuel du contexte. Remplis entry uniquement \
pour OPEN_*, adjustment uniquement pour ADJUST ; sinon mets null.
Rédige thesis, key_factors, risks et invalidation en français, de façon concise."""


class ClaudeTrader:
    """Appelle Claude pour une décision d'entrée ou une revue de position."""

    def __init__(self, claude: ClaudeClient, *, decision_model: str, decision_effort: str,
                 review_model: str) -> None:
        self.claude = claude
        self.decision_model = decision_model
        self.decision_effort = decision_effort
        self.review_model = review_model

    def decide(self, context: dict[str, Any], *, review: bool) -> tuple[TradeDecision, str]:
        model = self.review_model if review else self.decision_model
        request = ("REVUE D'UNE POSITION OUVERTE. " if review else "ANALYSE D'UNE OPPORTUNITÉ. ") + \
            "Contexte complet (JSON) :\n\n" + json.dumps(context, ensure_ascii=False, default=str)
        decision = self.claude.structured(
            purpose="revue" if review else "décision", model=model,
            effort="low" if review else self.decision_effort, system=SYSTEM_PROMPT, user=request,
            output_model=TradeDecision, estimate_usd=0.08 if review else 0.25,
        )
        return decision, model
