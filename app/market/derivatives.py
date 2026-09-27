"""Données publiques « vraie vie » complémentaires (lecture seule, sans clé API).

- Binance Futures (``fapi.binance.com``) : taux de financement, open interest et son
  évolution, ratio de comptes long/short, ratio acheteurs/vendeurs agressifs.
- alternative.me : indice Crypto Fear & Greed.

Chaque source est optionnelle : en cas d'indisponibilité le bot continue sans elle.
Dépendances externes remplaçables : toute classe exposant les mêmes méthodes convient.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.market.data_provider import MarketDataError, ReadOnlyHttpClient

logger = logging.getLogger(__name__)

FUTURES_MARKET_PATHS = frozenset({
    "/fapi/v1/premiumIndex", "/futures/data/openInterestHist",
    "/futures/data/globalLongShortAccountRatio", "/futures/data/takerlongshortRatio",
})


class BinanceFuturesData:
    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = ReadOnlyHttpClient(base_url, timeout, transport, FUTURES_MARKET_PATHS)

    def snapshot(self, symbol: str) -> dict[str, Any]:
        premium = self._http.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = self._http.get("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25})
        ratio = self._http.get("/futures/data/globalLongShortAccountRatio",
                               {"symbol": symbol, "period": "1h", "limit": 2})
        taker = self._http.get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "1h", "limit": 4})
        oi_now, oi_day = float(oi[-1]["sumOpenInterestValue"]), float(oi[0]["sumOpenInterestValue"])
        return {
            "funding_rate": float(premium["lastFundingRate"]),
            "mark_price": float(premium["markPrice"]),
            "basis_pct": float(premium["markPrice"]) / float(premium["indexPrice"]) - 1,
            "open_interest_usd": oi_now,
            "open_interest_change_24h": oi_now / oi_day - 1 if oi_day else 0.0,
            "long_short_account_ratio": float(ratio[-1]["longShortRatio"]),
            "taker_buy_sell_ratio_4h": sum(float(t["buySellRatio"]) for t in taker) / len(taker),
        }


class FearGreedIndex:
    def __init__(self, base_url: str = "https://api.alternative.me", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = ReadOnlyHttpClient(base_url, timeout, transport, frozenset({"/fng/"}))

    def latest(self) -> dict[str, Any]:
        data = self._http.get("/fng/", {"limit": 2})["data"]
        return {"value": int(data[0]["value"]), "label": data[0]["value_classification"],
                "previous": int(data[1]["value"]) if len(data) > 1 else None}


def safe_call(label: str, fn: Any, *args: Any) -> Any:
    """Appelle une source optionnelle ; retourne ``None`` (et journalise) en cas d'échec."""
    try:
        return fn(*args)
    except (MarketDataError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Source %s indisponible : %s", label, exc)
        return None
