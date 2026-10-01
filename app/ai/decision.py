"""Décision de trading par Claude : schéma de sortie structurée et consignes.

Claude choisit l'action (entrer, tenir, ajuster, clôturer), l'horizon, le stop, les
objectifs, la prise de profit partielle et le pourcentage de capital risqué. Le
``RiskManager`` vérifie ensuite chaque chiffre : Claude ne peut ni dépasser le risque
maximal, ni élargir un stop, ni contourner les limites de compte.
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
    horizon: Literal["intraday", "swing", "long_terme"]


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


class TradeLesson(BaseModel):
    lesson: str
    what_worked: str
    what_failed: str
    rule_for_next_time: str


SYSTEM_PROMPT = """Tu es le gérant d'un portefeuille multi-marchés (crypto, actions US, ETF, forex) \
piloté par un bot en PAPER TRADING (argent simulé, mêmes règles, frais et horaires qu'un compte réel). \
Tu décides à partir d'un contexte complet : analyse multi-timeframe (tendance, RSI 7/14/21 et \
divergences, ADX, ATR, MACD, Bollinger), volumes (volume relatif, MFI, OBV), supports et \
résistances, milieu d'influence de l'actif (indices, VIX, taux US, dollar, secteur), dérivés et \
carnet d'ordres pour la crypto, sessions et heures d'ouverture, actualités et calendrier économique, \
portefeuille, et un panorama des autres marchés.

Objectif : le meilleur taux de réussite possible AVEC une espérance positive après frais, en \
protégeant le capital avant tout. Ne prends que les configurations à forte probabilité, avec plusieurs \
facteurs concordants (tendance multi-timeframe, momentum RSI, confirmation par le volume, contexte \
d'influence favorable). « HOLD » est une réponse normale et fréquente. Compare avec le panorama : si \
une autre opportunité est clairement meilleure, ne gaspille pas ton budget de risque ici.

Règles :
- Entrée : stop derrière un niveau technique, entre 0,5 et 6 ATR du prix ; objectif final au moins \
1,5 fois le risque, sur un niveau réaliste. Prise de profit partielle (fraction 0,3 à 0,6) entre \
l'entrée et l'objectif pour sécuriser : le stop passe alors au point mort.
- horizon : « intraday » (clôture sous 24 h, time_stop_hours <= 24), « swing » (quelques jours, \
<= 168 h) ou « long_terme » (tendance de fond hebdomadaire/journalière, jusqu'à 2 160 h, stops plus \
larges, objectifs plus lointains, risque plus faible). Adapte stop et objectif à l'horizon.
- risk_percent : 0,25 à 0,5 conviction modérée, 0,5 à 1 bonne configuration, 1 à 2 configuration \
exceptionnelle seulement. Réduis-le en drawdown, après des pertes, en forte incertitude macro, sur un \
marché peu liquide ou pour le long terme. Le système peut le réduire, jamais l'augmenter.
- Volume : un mouvement sans volume est suspect ; une divergence RSI contre ta position est un \
signal d'alerte. Forex : pas de volume, appuie-toi sur les taux, le dollar et le calendrier.
- Actions et ETF : tiens compte des résultats d'entreprise à venir et de l'ouverture/clôture de la \
séance ; évite de porter une position de court terme à travers une publication de résultats.
- Revue de position : HOLD si la thèse tient, ADJUST pour resserrer le stop (jamais l'élargir) ou \
déplacer l'objectif, CLOSE si la thèse est invalidée. Laisse courir les gagnants dont la thèse tient.
- confidence entre 0 et 1, calibrée : ton historique et tes leçons passées te sont fournis ; applique \
ces leçons et sois plus exigeant là où tu as perdu.
- Prix actuel : « prix_actuel.prix » (bougie en cours, c'est le prix d'un ordre au marché) ; les indicateurs portent sur les bougies clôturées. Stop, objectif et limite cohérents avec ce prix. entry uniquement pour OPEN_*, adjustment uniquement pour ADJUST, \
sinon null. Réponds en français, de façon concise."""

LESSON_PROMPT = """Tu analyses a posteriori un trade clôturé par ton bot de paper trading, pour en \
tirer une leçon réutilisable. Sois factuel et concis (français) : ce qui a marché, ce qui a échoué, \
et une règle concrète à appliquer la prochaine fois (conditions de marché précises)."""


class ClaudeTrader:
    """Appelle Claude pour une décision d'entrée, une revue de position ou une leçon."""

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

    def lesson(self, trade_report: dict[str, Any]) -> TradeLesson:
        return self.claude.structured(
            purpose="leçon", model=self.review_model, effort="low", system=LESSON_PROMPT,
            user="Trade clôturé (JSON) :\n\n" + json.dumps(trade_report, ensure_ascii=False, default=str),
            output_model=TradeLesson, estimate_usd=0.03, max_tokens=4_000,
        )
