"""Heures d'ouverture des marchés et régimes de liquidité.

La crypto cote 24 h/24, mais sa liquidité et sa volatilité dépendent des sessions
traditionnelles : Asie (Tokyo), Europe (Londres), États-Unis (NYSE), du chevauchement
Londres / New York, des week-ends, des jours fériés américains et des horaires du
future Bitcoin du CME (source des « gaps CME »). Pour un actif action (non crypto), les
entrées ne sont autorisées que pendant la séance régulière du NYSE.

Heures d'été / d'hiver gérées par ``zoneinfo`` ; jours fériés NYSE par ``holidays``.
Limite : les demi-séances (veille de Noël, lendemain de Thanksgiving…) ne sont pas modélisées.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

from app.core.types import ensure_utc

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")
CRYPTO_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "EUR")


@dataclass(frozen=True, slots=True)
class Session:
    name: str
    tz: ZoneInfo
    open: time
    close: time

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        return local.weekday() < 5 and self.open <= local.time() < self.close


SESSIONS = (
    Session("Sydney", ZoneInfo("Australia/Sydney"), time(7), time(16)),
    Session("Tokyo", ZoneInfo("Asia/Tokyo"), time(9), time(15)),
    Session("Londres", ZoneInfo("Europe/London"), time(8), time(16, 30)),
    Session("New York", NEW_YORK, time(9, 30), time(16)),
)


@dataclass(frozen=True, slots=True)
class MarketClock:
    utc: datetime
    active_sessions: list[str]
    weekend: bool
    us_holiday: str | None
    us_equity_open: bool
    next_us_open: datetime
    minutes_to_us_open: int | None
    minutes_since_us_open: int | None
    cme_btc_futures_open: bool
    liquidity: str  # élevée | normale | faible
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "utc": self.utc.isoformat(), "active_sessions": self.active_sessions, "weekend": self.weekend,
            "us_holiday": self.us_holiday, "us_equity_open": self.us_equity_open,
            "next_us_open": self.next_us_open.isoformat(), "minutes_to_us_open": self.minutes_to_us_open,
            "minutes_since_us_open": self.minutes_since_us_open,
            "cme_btc_futures_open": self.cme_btc_futures_open, "liquidity": self.liquidity, "notes": self.notes,
        }


@lru_cache(maxsize=8)
def _nyse_holidays(year: int) -> holidays.HolidayBase:
    return holidays.financial_holidays("NYSE", years=[year])


def us_holiday(day: date) -> str | None:
    return _nyse_holidays(day.year).get(day)


def is_nyse_trading_day(day: date) -> bool:
    return day.weekday() < 5 and us_holiday(day) is None


def us_equity_open(now: datetime) -> bool:
    local = ensure_utc(now).astimezone(NEW_YORK)
    return is_nyse_trading_day(local.date()) and time(9, 30) <= local.time() < time(16)


def next_us_open(now: datetime) -> datetime:
    local = ensure_utc(now).astimezone(NEW_YORK)
    day = local.date()
    if local.time() >= time(9, 30):
        day += timedelta(days=1)
    while not is_nyse_trading_day(day):
        day += timedelta(days=1)
    return datetime.combine(day, time(9, 30), NEW_YORK).astimezone(UTC)


def cme_btc_futures_open(now: datetime) -> bool:
    """Future BTC du CME : dimanche 17h → vendredi 16h (Chicago), pause quotidienne 16h-17h."""
    local = ensure_utc(now).astimezone(CHICAGO)
    weekday, t = local.weekday(), local.time()
    if weekday == 5 or (weekday == 4 and t >= time(16)) or (weekday == 6 and t < time(17)):
        return False
    return not time(16) <= t < time(17)


def market_clock(now: datetime) -> MarketClock:
    now = ensure_utc(now)
    us_open = us_equity_open(now)
    active = [s.name for s in SESSIONS if s.is_open(now) and (s.tz is not NEW_YORK or us_open)]
    weekend = now.weekday() >= 5
    holiday = us_holiday(now.astimezone(NEW_YORK).date())
    upcoming = next_us_open(now)
    session_start = datetime.combine(now.astimezone(NEW_YORK).date(), time(9, 30), NEW_YORK)
    notes: list[str] = []
    if "Londres" in active and "New York" in active:
        liquidity = "élevée"
        notes.append("Chevauchement Londres / New York : liquidité et volatilité maximales")
    elif weekend or not active:
        liquidity = "faible"
        notes.append("Week-end ou hors sessions : liquidité réduite, mèches plus probables")
    else:
        liquidity = "normale"
    if holiday:
        notes.append(f"Jour férié NYSE : {holiday} (marché actions US fermé)")
    minutes_to_open = int((upcoming - now).total_seconds() // 60)
    if not us_open and minutes_to_open <= 60:
        notes.append(f"Ouverture de Wall Street dans {minutes_to_open} min : volatilité attendue")
    if not cme_btc_futures_open(now):
        notes.append("Future BTC CME fermé : un gap CME peut se former à la réouverture")
    return MarketClock(
        utc=now, active_sessions=active, weekend=weekend, us_holiday=holiday, us_equity_open=us_open,
        next_us_open=upcoming, minutes_to_us_open=None if us_open else minutes_to_open,
        minutes_since_us_open=int((now - session_start).total_seconds() // 60) if us_open else None,
        cme_btc_futures_open=cme_btc_futures_open(now), liquidity=liquidity, notes=notes,
    )


def is_crypto(symbol: str) -> bool:
    return symbol.upper().endswith(CRYPTO_QUOTES)


def entry_window_block(symbol: str, now: datetime, *, trade_weekends: bool,
                       no_trade_hours_utc: frozenset[int]) -> str | None:
    """Raison de bloquer une nouvelle entrée à cette heure, ou ``None`` si autorisé."""
    now = ensure_utc(now)
    if not is_crypto(symbol) and not us_equity_open(now):
        return "Marché actions US fermé"
    if not trade_weekends and now.weekday() >= 5:
        return "Trading du week-end désactivé (TRADE_WEEKENDS=false)"
    if now.hour in no_trade_hours_utc:
        return f"Heure {now.hour:02d}h UTC exclue (NO_TRADE_HOURS_UTC)"
    return None
