"""Briefing « vraie vie » : Claude cherche sur le web actualités, calendrier économique et sentiment.

Le briefing alimente chaque décision de trading et définit des fenêtres de blackout
(pas de nouvelle entrée autour des annonces à fort impact : CPI, emploi US, FOMC...).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.ai.claude import ClaudeClient
from app.core.types import ensure_utc, utcnow

logger = logging.getLogger(__name__)


class BriefingEvent(BaseModel):
    time_utc: str
    name: str
    impact: Literal["low", "medium", "high"]
    region: str


class BlackoutWindow(BaseModel):
    start_utc: datetime
    end_utc: datetime
    reason: str

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SymbolView(BaseModel):
    symbol: str
    bias: Literal["bullish", "bearish", "neutral"]
    notes: str


class MarketBriefing(BaseModel):
    generated_at: datetime = Field(default_factory=utcnow)
    model: str = ""
    overall_sentiment: float = Field(0.0, ge=-1, le=1)
    risk_level: Literal["low", "medium", "high", "extreme"] = "medium"
    summary: str = ""
    key_events: list[BriefingEvent] = Field(default_factory=list)
    blackout_windows: list[BlackoutWindow] = Field(default_factory=list)
    per_symbol: list[SymbolView] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

    def active_blackout(self, now: datetime) -> BlackoutWindow | None:
        return next((w for w in self.blackout_windows if w.start_utc <= now <= w.end_utc), None)

    def for_prompt(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"sources"})


SUBMIT_BRIEFING_TOOL: dict[str, Any] = {
    "name": "submit_briefing",
    "description": "Soumet le briefing de marché final (à appeler une seule fois, après les recherches).",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["overall_sentiment", "risk_level", "summary", "key_events", "blackout_windows",
                     "per_symbol", "sources"],
        "properties": {
            "overall_sentiment": {"type": "number", "description": "-1 très baissier à +1 très haussier"},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high", "extreme"]},
            "summary": {"type": "string", "description": "Synthèse en français, 10 lignes maximum"},
            "key_events": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["time_utc", "name", "impact", "region"],
                "properties": {
                    "time_utc": {"type": "string", "description": "ISO 8601 UTC, vide si inconnue"},
                    "name": {"type": "string"},
                    "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                    "region": {"type": "string"}}}},
            "blackout_windows": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["start_utc", "end_utc", "reason"],
                "properties": {"start_utc": {"type": "string", "description": "ISO 8601 UTC"},
                               "end_utc": {"type": "string", "description": "ISO 8601 UTC"},
                               "reason": {"type": "string"}}}},
            "per_symbol": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["symbol", "bias", "notes"],
                "properties": {"symbol": {"type": "string"},
                               "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
                               "notes": {"type": "string"}}}},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
    },
}

SYSTEM_PROMPT = """Tu es l'analyste macro et crypto d'un bot de trading (paper trading). Ton travail : \
produire un briefing factuel et à jour qui servira aux décisions de trading des prochaines heures.

Utilise la recherche web pour vérifier l'information récente. Ne devine jamais une date ou une \
heure : si tu ne la trouves pas, laisse time_utc vide. Distingue les faits des opinions. Les \
fenêtres de blackout couvrent les annonces à fort impact (par ex. CPI, emploi US, décision et \
conférence FOMC, PCE, discours majeurs de la Fed) des 36 prochaines heures, de 30 minutes avant \
à 30 minutes après l'annonce, en UTC. Termine TOUJOURS en appelant l'outil submit_briefing."""


def briefing_request(symbols: list[str], now: datetime) -> str:
    return (f"Nous sommes le {now:%Y-%m-%d %H:%M} UTC. Actifs suivis : {', '.join(symbols)}.\n"
            "Recherche : (1) les actualités crypto et macro des dernières 24 heures qui peuvent faire "
            "bouger ces actifs (ETF, régulation, piratages, liquidations, flux, décisions de banques "
            "centrales) ; (2) le calendrier économique US et européen des 36 prochaines heures avec les "
            "heures UTC ; (3) le sentiment général du marché. Puis appelle submit_briefing.")


class BriefingService:
    def __init__(self, claude: ClaudeClient, *, model: str, symbols: list[str], interval_minutes: int,
                 max_searches: int, blackout_minutes: int = 30, on_briefing: Any = None) -> None:
        self.claude = claude
        self.model = model
        self.symbols = symbols
        self.interval = timedelta(minutes=interval_minutes)
        self.max_searches = max_searches
        self.blackout_minutes = blackout_minutes
        self.latest: MarketBriefing | None = None
        self._on_briefing = on_briefing
        self._lock = asyncio.Lock()

    def due(self, now: datetime) -> bool:
        return self.latest is None or now - self.latest.generated_at >= self.interval

    def blackout(self, now: datetime) -> BlackoutWindow | None:
        return self.latest.active_blackout(now) if self.latest else None

    async def ensure_ready(self) -> None:
        """Attend le premier briefing (ou celui en cours) avant une décision."""
        if self.latest is None or self._lock.locked():
            await self.refresh(only_if_due=True)

    async def refresh(self, only_if_due: bool = False) -> MarketBriefing | None:
        async with self._lock:
            now = datetime.now(UTC)
            if only_if_due and not self.due(now):
                return self.latest
            data = await asyncio.to_thread(
                self.claude.research, purpose="briefing", model=self.model, effort="medium",
                system=SYSTEM_PROMPT, user=briefing_request(self.symbols, now), submit_tool=SUBMIT_BRIEFING_TOOL,
                max_searches=self.max_searches, estimate_usd=0.02 * self.max_searches + 0.15)
            self.latest = parse_briefing(data, self.model, now, self.blackout_minutes)
            logger.info("Briefing de marché : risque %s, sentiment %+.2f, %d événement(s), %d blackout(s)",
                        self.latest.risk_level, self.latest.overall_sentiment, len(self.latest.key_events),
                        len(self.latest.blackout_windows))
            if self._on_briefing is not None:
                self._on_briefing(self.latest)
            return self.latest


def parse_briefing(data: dict[str, Any], model: str, now: datetime,
                   blackout_minutes: int = 30) -> MarketBriefing:
    """Valide la réponse de Claude.

    Les fenêtres de blackout illisibles sont ignorées ; tout événement à fort impact daté
    en ajoute une (± ``blackout_minutes``), même si Claude ne l'a pas déclarée.
    """
    candidates: list[dict[str, Any]] = list(data.get("blackout_windows", []))
    margin = timedelta(minutes=blackout_minutes)
    for event in data.get("key_events", []):
        try:
            when = ensure_utc(datetime.fromisoformat(str(event.get("time_utc", "")).replace("Z", "+00:00")))
        except ValueError:
            continue
        if event.get("impact") == "high":
            candidates.append({"start_utc": when - margin, "end_utc": when + margin,
                               "reason": f"Annonce à fort impact : {event.get('name', '?')}"})
    windows = []
    for window in candidates:
        try:
            parsed = BlackoutWindow.model_validate(window)
        except ValueError:
            logger.warning("Fenêtre de blackout ignorée : %s", window)
            continue
        if parsed.end_utc > now and parsed.end_utc - parsed.start_utc <= timedelta(hours=6):
            windows.append(parsed)
    sentiment = max(-1.0, min(1.0, float(data.get("overall_sentiment", 0.0))))
    return MarketBriefing(
        generated_at=now, model=model, overall_sentiment=sentiment, risk_level=data.get("risk_level", "medium"),
        summary=data.get("summary", ""), key_events=data.get("key_events", []), blackout_windows=windows,
        per_symbol=data.get("per_symbol", []), sources=data.get("sources", [])[:20],
    )
