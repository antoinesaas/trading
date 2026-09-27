"""Sources de données de marché (abstraction ``MarketDataProvider``).

Dépendance externe : l'API publique de données de marché Binance
(``https://data-api.binance.vision``), sans clé API, en lecture seule. Elle est
remplaçable par n'importe quelle implémentation de ``MarketDataProvider`` (CSV,
autre exchange, flux TradingView tiers...).
"""

from __future__ import annotations

import csv
import logging
import math
import random
from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.core.types import Candle, ensure_utc
from app.market.timeframes import timeframe_seconds
from app.safety import LiveTradingDisabledError

logger = logging.getLogger(__name__)

BINANCE_MAX_LIMIT = 1_000
CSV_FIELDS = ("open_time", "open", "high", "low", "close", "volume")


class MarketDataError(RuntimeError):
    """Données de marché indisponibles ou invalides."""


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        """Dernières bougies, triées. La dernière peut être en formation (``closed=False``)."""

    def fetch_history(self, symbol: str, timeframe: str, bars: int) -> list[Candle]:
        """Historique de ``bars`` bougies clôturées (utilisé par backtests et optimiseur)."""
        candles = [c for c in self.fetch_candles(symbol, timeframe, bars + 1) if c.closed]
        return candles[-bars:]


SPOT_MARKET_PATHS = frozenset({"/api/v3/klines", "/api/v3/ticker/24hr", "/api/v3/depth"})


class ReadOnlyHttpClient:
    """Client HTTP limité à une liste blanche de chemins GET de données publiques.

    Il n'expose volontairement aucune méthode POST/PUT/DELETE : aucun ordre ne peut
    transiter par ce client.
    """

    def __init__(self, base_url: str, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None,
                 allowed_paths: frozenset[str] = SPOT_MARKET_PATHS,
                 allowed_prefixes: tuple[str, ...] = (), headers: dict[str, str] | None = None) -> None:
        self.allowed_paths = allowed_paths
        self.allowed_prefixes = allowed_prefixes
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, headers=headers)

    def get(self, path: str, params: dict[str, Any]) -> Any:
        allowed = path in self.allowed_paths or (path.startswith(self.allowed_prefixes) and ".." not in path)
        if not allowed:
            raise LiveTradingDisabledError(f"Requête HTTP vers {path!r} interdite (lecture seule).")
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"Erreur de données de marché ({path}) : {exc}") from exc

    def close(self) -> None:
        self._client.close()


class BinanceMarketDataProvider(MarketDataProvider):
    name = "binance"

    def __init__(self, base_url: str, timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = ReadOnlyHttpClient(base_url, timeout, transport)

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500,
                      end_time: datetime | None = None) -> list[Candle]:
        timeframe_seconds(timeframe)  # valide le timeframe
        params: dict[str, Any] = {"symbol": symbol, "interval": timeframe,
                                  "limit": max(1, min(limit, BINANCE_MAX_LIMIT))}
        if end_time is not None:
            params["endTime"] = int(ensure_utc(end_time).timestamp() * 1000)
        rows = self._http.get("/api/v3/klines", params)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return [self._parse_row(symbol, timeframe, row, now_ms) for row in rows]

    def fetch_history(self, symbol: str, timeframe: str, bars: int) -> list[Candle]:
        collected: dict[datetime, Candle] = {}
        end_time: datetime | None = None
        while len(collected) < bars:
            page = self.fetch_candles(symbol, timeframe, min(BINANCE_MAX_LIMIT, bars + 1), end_time)
            page = [c for c in page if c.closed and c.open_time not in collected]
            if not page:
                break
            collected.update((c.open_time, c) for c in page)
            end_time = page[0].open_time - timedelta(milliseconds=1)
        return sorted(collected.values(), key=lambda c: c.open_time)[-bars:]

    def ticker_24h(self, symbol: str) -> dict[str, float]:
        data = self._http.get("/api/v3/ticker/24hr", {"symbol": symbol})
        return {key: float(data[key]) for key in ("lastPrice", "priceChangePercent", "highPrice", "lowPrice",
                                                   "quoteVolume", "weightedAvgPrice")}

    def order_book(self, symbol: str, depth: int = 100) -> dict[str, float]:
        """Spread et déséquilibre acheteurs/vendeurs sur les meilleurs niveaux du carnet."""
        data = self._http.get("/api/v3/depth", {"symbol": symbol, "limit": depth})
        bids = [(float(p), float(q)) for p, q in data["bids"]]
        asks = [(float(p), float(q)) for p, q in data["asks"]]
        if not bids or not asks:
            raise MarketDataError(f"Carnet d'ordres vide pour {symbol}")
        mid = (bids[0][0] + asks[0][0]) / 2
        near = mid * 0.005  # liquidité à ±0,5 % du prix
        bid_depth = sum(p * q for p, q in bids if p >= mid - near)
        ask_depth = sum(p * q for p, q in asks if p <= mid + near)
        total = bid_depth + ask_depth
        return {"mid": mid, "spread_pct": (asks[0][0] - bids[0][0]) / mid,
                "bid_depth_quote": bid_depth, "ask_depth_quote": ask_depth,
                "imbalance": (bid_depth - ask_depth) / total if total else 0.0}

    @staticmethod
    def _parse_row(symbol: str, timeframe: str, row: Sequence[Any], now_ms: int) -> Candle:
        try:
            open_ms, close_ms = int(row[0]), int(row[6])
            candle = Candle(
                symbol=symbol, timeframe=timeframe,
                open_time=datetime.fromtimestamp(open_ms / 1000, UTC),
                close_time=datetime.fromtimestamp((close_ms + 1) / 1000, UTC),
                open=float(row[1]), high=float(row[2]), low=float(row[3]), close=float(row[4]),
                volume=float(row[5]), closed=close_ms < now_ms,
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise MarketDataError(f"Bougie Binance invalide : {row!r}") from exc
        validate_candle(candle)
        return candle


class CSVMarketDataProvider(MarketDataProvider):
    """Lit ``{SYMBOL}_{timeframe}.csv`` dans un dossier (données figées = reproductibles)."""

    name = "csv"

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._cache: dict[tuple[str, str], list[Candle]] = {}

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self._directory / f"{symbol}_{timeframe}.csv"

    def fetch_candles(self, symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
        key = (symbol, timeframe)
        if key not in self._cache:
            self._cache[key] = load_candles_csv(self.path_for(symbol, timeframe), symbol, timeframe)
        return self._cache[key][-limit:]


def validate_candle(candle: Candle) -> None:
    values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
    if not all(math.isfinite(v) for v in values):
        raise MarketDataError(f"Bougie non finie : {candle}")
    if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0:
        raise MarketDataError(f"Bougie avec prix/volume invalide : {candle}")
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        raise MarketDataError(f"Bougie incohérente (high/low) : {candle}")


def _parse_time(raw: str) -> datetime:
    raw = raw.strip()
    if raw.isdigit():
        value = int(raw)
        return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
    return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))


def load_candles_csv(path: Path, symbol: str, timeframe: str) -> list[Candle]:
    if not path.exists():
        raise MarketDataError(f"Fichier de données introuvable : {path}")
    step = timedelta(seconds=timeframe_seconds(timeframe))
    candles: list[Candle] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            try:
                open_time = _parse_time(row["open_time"])
                candle = Candle(symbol, timeframe, open_time, open_time + step,
                                float(row["open"]), float(row["high"]), float(row["low"]),
                                float(row["close"]), float(row["volume"]))
            except (KeyError, ValueError) as exc:
                raise MarketDataError(f"{path}:{line_no} ligne invalide ({exc})") from exc
            validate_candle(candle)
            candles.append(candle)
    candles.sort(key=lambda c: c.open_time)
    return candles


def save_candles_csv(path: Path, candles: Sequence[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for c in candles:
            writer.writerow((c.open_time.isoformat(), c.open, c.high, c.low, c.close, c.volume))


def generate_synthetic_candles(symbol: str = "SYNTHUSDT", timeframe: str = "1h", bars: int = 1_000,
                               seed: int = 42, start_price: float = 100.0,
                               start_time: datetime = datetime(2024, 1, 1, tzinfo=UTC),
                               volatility: float = 0.006) -> list[Candle]:
    """Série déterministe à régimes (hausse / baisse / range) pour tests et démonstrations."""
    rng = random.Random(seed)
    step = timedelta(seconds=timeframe_seconds(timeframe))
    candles: list[Candle] = []
    price, drift, regime_left = start_price, 0.0, 0
    for i in range(bars):
        if regime_left <= 0:
            drift = rng.choice((0.0015, -0.0015, 0.0))
            regime_left = rng.randint(60, 180)
        regime_left -= 1
        open_price = price
        close_price = open_price * math.exp(drift + rng.gauss(0, volatility))
        wick = abs(rng.gauss(0, volatility / 2))
        high = max(open_price, close_price) * (1 + wick)
        low = min(open_price, close_price) * (1 - abs(rng.gauss(0, volatility / 2)))
        open_time = start_time + i * step
        candles.append(Candle(symbol, timeframe, open_time, open_time + step, open_price, high, low,
                              close_price, rng.uniform(50, 150)))
        price = close_price
    return candles
