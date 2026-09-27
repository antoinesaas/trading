"""Conversions de timeframes (format Binance / TradingView)."""

from __future__ import annotations

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1_800,
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "6h": 21_600,
    "8h": 28_800,
    "12h": 43_200,
    "1d": 86_400,
}

_SECONDS_PER_YEAR = 365 * 24 * 3_600


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        supported = ", ".join(TIMEFRAME_SECONDS)
        raise ValueError(f"Timeframe {timeframe!r} non supporté ({supported}).") from exc


def periods_per_year(timeframe: str) -> float:
    """Nombre de bougies par an, marché ouvert 24/7 (crypto)."""
    return _SECONDS_PER_YEAR / timeframe_seconds(timeframe)
