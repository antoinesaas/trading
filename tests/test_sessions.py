from datetime import UTC, datetime

from app.market.sessions import (
    cme_btc_futures_open, entry_window_block, is_crypto, market_clock, next_us_open, us_equity_open,
)


def test_us_equity_hours_follow_daylight_saving():
    # Heure d'été US (EDT, UTC-4) : ouverture 13h30 UTC ; heure d'hiver (EST, UTC-5) : 14h30 UTC.
    assert us_equity_open(datetime(2026, 9, 28, 13, 31, tzinfo=UTC))
    assert not us_equity_open(datetime(2026, 9, 28, 13, 29, tzinfo=UTC))
    assert not us_equity_open(datetime(2026, 12, 7, 14, 0, tzinfo=UTC))
    assert us_equity_open(datetime(2026, 12, 7, 14, 31, tzinfo=UTC))


def test_nyse_holidays_and_weekends_are_closed():
    thanksgiving = datetime(2026, 11, 26, 16, 0, tzinfo=UTC)
    assert not us_equity_open(thanksgiving)
    clock = market_clock(thanksgiving)
    assert clock.us_holiday == "Thanksgiving Day" and "New York" not in clock.active_sessions
    assert next_us_open(datetime(2026, 9, 26, 12, 0, tzinfo=UTC)) == datetime(2026, 9, 28, 13, 30, tzinfo=UTC)


def test_liquidity_regimes_and_cme():
    overlap = market_clock(datetime(2026, 9, 28, 14, 0, tzinfo=UTC))
    assert overlap.liquidity == "élevée" and {"Londres", "New York"} <= set(overlap.active_sessions)
    weekend = market_clock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
    assert weekend.liquidity == "faible" and weekend.weekend and not weekend.cme_btc_futures_open
    assert cme_btc_futures_open(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    assert not cme_btc_futures_open(datetime(2026, 9, 29, 21, 30, tzinfo=UTC))  # pause 16h-17h Chicago


def test_entry_window_rules():
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert is_crypto("BTCUSDT") and not is_crypto("AAPL")
    assert entry_window_block("BTCUSDT", saturday, trade_weekends=True, no_trade_hours_utc=frozenset()) is None
    assert "week-end" in entry_window_block("BTCUSDT", saturday, trade_weekends=False,
                                            no_trade_hours_utc=frozenset())
    assert "12h" in entry_window_block("BTCUSDT", saturday, trade_weekends=True,
                                       no_trade_hours_utc=frozenset({12}))
    assert "fermé" in entry_window_block("AAPL", saturday, trade_weekends=True, no_trade_hours_utc=frozenset())
