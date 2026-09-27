"""Contexte complet transmis à Claude avant chaque décision.

Il réunit, pour un symbole : l'analyse multi-timeframe (tendance, momentum, volatilité,
force de tendance, niveaux), les dernières bougies, le carnet d'ordres, les statistiques
24 h, les dérivés (funding, open interest, ratio long/short), le Fear & Greed, les
sessions de marché, le briefing d'actualité et l'état du portefeuille. Chaque source est
optionnelle : une source indisponible est signalée comme telle.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np

from app.core.types import Candle
from app.market.data_provider import MarketDataError, MarketDataProvider
from app.market.derivatives import BinanceFuturesData, FearGreedIndex, safe_call
from app.market.indicators import adx, atr, bollinger, ema, macd, rsi, swing_levels, zscore

logger = logging.getLogger(__name__)
RECENT_CANDLES = 24
HISTORY_BARS = 260


def _r(value: float, digits: int = 6) -> float | None:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def _pct(a: float, b: float) -> float | None:
    return _r((a / b - 1) * 100, 3) if b else None


def summarize_timeframe(candles: list[Candle]) -> dict[str, Any]:
    """Photographie technique d'un timeframe (bougies clôturées uniquement)."""
    if len(candles) < 60:
        return {"disponible": False, "raison": f"{len(candles)} bougies seulement"}
    close = np.array([c.close for c in candles])
    high = np.array([c.high for c in candles])
    low = np.array([c.low for c in candles])
    volume = np.array([c.volume for c in candles])
    e20, e50, e200 = ema(close, 20)[-1], ema(close, 50)[-1], ema(close, 200)[-1]
    atr14 = atr(high, low, close, 14)[-1]
    macd_line, signal_line, hist = macd(close)
    _, upper, lower = bollinger(close)
    c = close[-1]
    trend = ("haussière" if c > e20 > e50 else "baissière" if c < e20 < e50 else "neutre / range")
    supports, resistances = swing_levels(high[-150:], low[-150:])
    return {
        "derniere_cloture": _r(c), "tendance": trend,
        "ema20": _r(e20), "ema50": _r(e50), "ema200": _r(e200),
        "rsi14": _r(rsi(close, 14)[-1], 1), "adx14": _r(adx(high, low, close, 14)[-1], 1),
        "atr14": _r(atr14), "atr_pct": _r(atr14 / c * 100, 3),
        "macd_hist": _r(hist[-1]), "macd_hist_precedent": _r(hist[-2]),
        "bollinger_pct_b": _r((c - lower[-1]) / (upper[-1] - lower[-1]), 3) if upper[-1] > lower[-1] else None,
        "bollinger_largeur_pct": _r((upper[-1] - lower[-1]) / c * 100, 3),
        "volume_zscore": _r(zscore(volume)[-1], 2),
        "variation_1_bougie_pct": _pct(c, close[-2]), "variation_6_bougies_pct": _pct(c, close[-7]),
        "variation_24_bougies_pct": _pct(c, close[-25]),
        "plus_haut_20": _r(high[-20:].max()), "plus_bas_20": _r(low[-20:].min()),
        "supports_recents": [_r(v) for v in supports], "resistances_recentes": [_r(v) for v in resistances],
    }


def compact_candles(candles: list[Candle]) -> list[list[Any]]:
    return [[c.open_time.strftime("%m-%d %H:%M"), _r(c.open), _r(c.high), _r(c.low), _r(c.close),
             _r(c.volume, 2)] for c in candles[-RECENT_CANDLES:]]


class MarketContextBuilder:
    def __init__(self, provider: MarketDataProvider, *, timeframes: list[str],
                 futures: BinanceFuturesData | None = None, fear_greed: FearGreedIndex | None = None) -> None:
        self.provider = provider
        self.timeframes = timeframes
        self.futures = futures
        self.fear_greed = fear_greed
        self._fng_cache: tuple[float, Any] | None = None

    def build(self, symbol: str, decision_timeframe: str) -> dict[str, Any]:
        """Contexte de marché (appel réseau : à exécuter hors de la boucle d'événements)."""
        frames: dict[str, Any] = {}
        recent: list[list[Any]] = []
        for tf in dict.fromkeys([decision_timeframe, *self.timeframes]):
            try:
                candles = [c for c in self.provider.fetch_candles(symbol, tf, HISTORY_BARS + 1) if c.closed]
            except MarketDataError as exc:
                frames[tf] = {"disponible": False, "raison": str(exc)}
                continue
            frames[tf] = summarize_timeframe(candles)
            if tf == decision_timeframe:
                recent = compact_candles(candles)
        context: dict[str, Any] = {
            "symbole": symbol, "timeframe_de_decision": decision_timeframe,
            "analyse_multi_timeframe": frames,
            "dernieres_bougies": {"colonnes": ["ouverture", "open", "high", "low", "close", "volume"],
                                  "valeurs": recent},
        }
        ticker = getattr(self.provider, "ticker_24h", None)
        book = getattr(self.provider, "order_book", None)
        context["stats_24h"] = safe_call("ticker 24h", ticker, symbol) if ticker else None
        context["carnet_ordres"] = safe_call("carnet d'ordres", book, symbol) if book else None
        context["derives"] = safe_call("dérivés", self.futures.snapshot, symbol) if self.futures else None
        context["fear_greed"] = self._fear_greed()
        return context

    def _fear_greed(self) -> Any:
        if self.fear_greed is None:
            return None
        if self._fng_cache is None or time.monotonic() - self._fng_cache[0] > 1_800:
            self._fng_cache = (time.monotonic(), safe_call("fear & greed", self.fear_greed.latest))
        return self._fng_cache[1]
