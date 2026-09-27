"""Données actions, ETF, indices et forex via l'API graphique publique de Yahoo Finance.

Dépendance externe : ``query1.finance.yahoo.com`` (sans clé, non officielle, en lecture
seule). Actions US quasi temps réel ; Euronext avec un léger différé ; forex en continu
5 j/7. Remplaçable par tout ``MarketDataProvider`` (Polygon, Twelve Data, broker...).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.types import Candle
from app.market.data_provider import MarketDataError, MarketDataProvider, ReadOnlyHttpClient, validate_candle
from app.market.timeframes import timeframe_seconds
from app.market.universe import DRIVER_NAMES, UNIVERSE

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
INTERVALS = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d", "1w": "1wk"}
MAX_RANGE_DAYS = {"5m": 59, "15m": 59, "30m": 59, "1h": 729, "1d": 3650, "1w": 7300}
BARS_PER_DAY = {"5m": 78, "15m": 26, "30m": 13, "1h": 7, "1d": 1, "1w": 0.2}


class YahooMarketData(MarketDataProvider):
    name = "yahoo"

    def __init__(self, base_url: str = "https://query1.finance.yahoo.com", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = ReadOnlyHttpClient(base_url, timeout, transport, allowed_prefixes=("/v8/finance/chart/",),
                                        headers={"User-Agent": USER_AGENT})

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        """``symbol`` : symbole du catalogue (ex. NVDA, CAC40, EURUSD) ou symbole Yahoo brut."""
        if timeframe == "4h":  # Yahoo ne fournit pas de 4h : agrégation des bougies 1h
            return resample(self.fetch_candles(symbol, "1h", limit * 5), "4h")[-limit:]
        interval = INTERVALS.get(timeframe)
        if interval is None:
            raise MarketDataError(f"Timeframe {timeframe} indisponible sur Yahoo Finance")
        days = min(MAX_RANGE_DAYS[timeframe], max(2, int(limit / BARS_PER_DAY[timeframe] * 1.6) + 5))
        provider_symbol = UNIVERSE[symbol].provider_symbol if symbol in UNIVERSE else symbol
        data = self._http.get(f"/v8/finance/chart/{provider_symbol}", {"interval": interval, "range": f"{days}d"})
        return parse_chart(symbol, timeframe, data)[-limit:]


def parse_chart(symbol: str, timeframe: str, data: dict[str, Any], now: datetime | None = None) -> list[Candle]:
    try:
        result = data["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        error = (data.get("chart") or {}).get("error") if isinstance(data, dict) else None
        raise MarketDataError(f"Réponse Yahoo invalide pour {symbol} : {error or exc}") from exc
    seconds = timeframe_seconds(timeframe)
    step = timedelta(seconds=seconds)
    now = now or datetime.now(UTC)
    offset = stamps[0] % seconds if stamps else 0  # ex. barres horaires US alignées sur :30
    candles: list[Candle] = []
    for i, stamp in enumerate(stamps):
        values = [quote[k][i] for k in ("open", "high", "low", "close")]
        if any(v is None for v in values):
            continue
        # Yahoo ajoute parfois un dernier point non aligné (mise à jour de la barre en cours).
        open_time = datetime.fromtimestamp(stamp - (stamp - offset) % seconds, UTC)
        o, h, lo, c = (float(v) for v in values)
        volume = float(quote["volume"][i] or 0.0)
        if candles and open_time <= candles[-1].open_time:
            if open_time == candles[-1].open_time:  # fusion avec la barre en cours
                last = candles[-1]
                candles[-1] = Candle(symbol, timeframe, last.open_time, last.close_time, last.open,
                                     max(last.high, h, c), min(last.low, lo, c), c, max(last.volume, volume),
                                     closed=last.close_time <= now)
            continue
        candle = Candle(symbol, timeframe, open_time, open_time + step, o, max(h, o, c), min(lo, o, c), c,
                        volume, closed=open_time + step <= now)
        validate_candle(candle)
        candles.append(candle)
    return candles


def resample(candles: list[Candle], timeframe: str, now: datetime | None = None) -> list[Candle]:
    """Agrège des bougies dans des tranches UTC de ``timeframe`` (ex. 1h -> 4h)."""
    seconds = timeframe_seconds(timeframe)
    now = now or datetime.now(UTC)
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        groups.setdefault(int(candle.open_time.timestamp()) // seconds, []).append(candle)
    result = []
    for bucket, members in sorted(groups.items()):
        start = datetime.fromtimestamp(bucket * seconds, UTC)
        end = start + timedelta(seconds=seconds)
        result.append(Candle(members[0].symbol, timeframe, start, end, members[0].open,
                             max(c.high for c in members), min(c.low for c in members), members[-1].close,
                             sum(c.volume for c in members), closed=end <= now and members[-1].closed))
    return result


class FxRates:
    """Taux de conversion vers la devise du compte (USD), rafraîchis toutes les 10 minutes."""

    PAIRS = {"EUR": ("EURUSD=X", False), "GBP": ("GBPUSD=X", False), "JPY": ("JPY=X", True),
             "CHF": ("CHF=X", True), "CAD": ("CAD=X", True)}
    USD_LIKE = frozenset({"USD", "USDT", "USDC", "FDUSD", "BUSD"})

    def __init__(self, yahoo: YahooMarketData | None = None, ttl_seconds: float = 600.0,
                 static: dict[str, float] | None = None) -> None:
        self._yahoo = yahoo
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, float]] = {}
        self._static = dict(static or {})
        self._lock = threading.Lock()

    def rate(self, currency: str) -> float:
        currency = currency.upper()
        if currency in self.USD_LIKE:
            return 1.0
        if currency in self._static:
            return self._static[currency]
        with self._lock:
            cached = self._cache.get(currency)
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
        pair = self.PAIRS.get(currency)
        if pair is None or self._yahoo is None:
            raise MarketDataError(f"Pas de taux de change pour {currency}")
        candles = self._yahoo.fetch_candles(pair[0], "1d", 5)
        if not candles:
            raise MarketDataError(f"Taux {currency} indisponible")
        value = 1 / candles[-1].close if pair[1] else candles[-1].close
        with self._lock:
            self._cache[currency] = (time.monotonic(), value)
        return value


class MultiMarketData(MarketDataProvider):
    """Aiguille chaque symbole vers sa source (Binance pour la crypto, Yahoo pour le reste)."""

    name = "multi"

    def __init__(self, binance: MarketDataProvider, yahoo: YahooMarketData) -> None:
        self.binance = binance
        self.yahoo = yahoo

    def _source(self, symbol: str) -> MarketDataProvider:
        if symbol in UNIVERSE:
            return self.binance if UNIVERSE[symbol].provider == "binance" else self.yahoo
        return self.yahoo if symbol in DRIVER_NAMES or not symbol.isalnum() else self.binance

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        return self._source(symbol).fetch_candles(symbol, timeframe, limit)

    def fetch_history(self, symbol: str, timeframe: str, bars: int) -> list[Candle]:
        return self._source(symbol).fetch_history(symbol, timeframe, bars)

    def ticker_24h(self, symbol: str) -> dict[str, float] | None:
        source = self._source(symbol)
        return source.ticker_24h(symbol) if hasattr(source, "ticker_24h") else None

    def order_book(self, symbol: str, depth: int = 100) -> dict[str, float] | None:
        source = self._source(symbol)
        return source.order_book(symbol, depth) if hasattr(source, "order_book") else None
