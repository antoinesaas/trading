"""Multi-marchés : données Yahoo, horaires Euronext/forex, devises, coûts par marché, volume, capital."""

from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest

from app.core.types import Candle, Direction, Signal
from app.market.data_provider import MarketDataError
from app.market.indicators import mfi, obv, rsi, rsi_divergence, zigzag
from app.market.sessions import entry_window_block, euronext_open, forex_open, market_open
from app.market.universe import UNIVERSE, instrument
from app.market.yahoo import FxRates, MultiMarketData, YahooMarketData, parse_chart, resample
from app.risk.risk_manager import AccountSnapshot, ExitParams, RiskManager
from app.trading.portfolio import Portfolio
from app.trading.specs import MarketSpecs
from tests.conftest import zero_cost_limits

H = 3600
# Lundi 28/09/2026 13:30 UTC : ouverture de Wall Street (barres horaires Yahoo alignées sur :30).
US_OPEN = int(datetime(2026, 9, 28, 13, 30, tzinfo=UTC).timestamp())


def chart_json(stamps, opens, highs, lows, closes, volumes):
    return {"chart": {"result": [{"timestamp": stamps, "indicators": {"quote": [
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}]}}], "error": None}}


# --- Yahoo Finance -------------------------------------------------------------------------

def test_parse_chart_aligns_bars_and_merges_the_live_point():
    stamps = [US_OPEN, US_OPEN + H, US_OPEN + 2 * H, US_OPEN + 2 * H + 1_234]  # dernier point : mise à jour
    data = chart_json(stamps, [10, None, 12, 12.5], [11, 12, 13, 13.6], [9.5, 10.5, 11.5, 12.2],
                      [11, 12, 13, 13.2], [100, 200, 300, 350])
    now = datetime.fromtimestamp(US_OPEN + 2 * H + 1_300, UTC)
    candles = parse_chart("NVDA", "1h", data, now=now)
    # La barre incomplète (valeur nulle) est ignorée ; le point en direct complète la barre de 15h30.
    assert [c.open_time for c in candles] == [datetime.fromtimestamp(t, UTC) for t in (US_OPEN, US_OPEN + 2 * H)]
    assert candles[0].closed and not candles[-1].closed
    assert (candles[-1].high, candles[-1].close, candles[-1].volume) == (13.6, 13.2, 350)
    with pytest.raises(MarketDataError):
        parse_chart("NVDA", "1h", {"chart": {"result": None, "error": {"code": "Not Found"}}})


def test_live_point_updates_the_forming_bar():
    stamps = [US_OPEN, US_OPEN + 1_800]  # le second point appartient encore à la barre de 13h30
    data = chart_json(stamps, [10, 10.4], [10.5, 11.2], [9.8, 10.3], [10.4, 11.0], [100, 150])
    (bar,) = parse_chart("NVDA", "1h", data, now=datetime.fromtimestamp(US_OPEN + 1_900, UTC))
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (10, 11.2, 9.8, 11.0, 150)


def test_resample_builds_utc_4h_bars():
    start = datetime(2026, 9, 28, 0, tzinfo=UTC)
    hourly = [Candle("X", "1h", start + timedelta(hours=i), start + timedelta(hours=i + 1), 100 + i, 101 + i,
                     99 + i, 100.5 + i, 10, closed=True) for i in range(8)]
    four = resample(hourly, "4h", now=start + timedelta(hours=9))
    assert len(four) == 2 and four[0].open == 100 and four[0].close == 103.5
    assert four[0].high == 104 and four[0].low == 99 and four[0].volume == 40 and four[1].closed


def test_yahoo_client_is_read_only_and_maps_symbols():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.params.get("interval"),
                     request.headers.get("user-agent")))
        return httpx.Response(200, json=chart_json([US_OPEN], [80], [81], [79], [80.5], [1000]))

    yahoo = YahooMarketData(transport=httpx.MockTransport(handler))
    yahoo.fetch_candles("CAC40", "1d", 10)
    yahoo.fetch_candles("NVDA", "4h", 10)  # pas de 4h chez Yahoo : 1h agrégé
    assert seen[0][:3] == ("GET", "/v8/finance/chart/CAC.PA", "1d") and seen[0][3]
    assert seen[1][1:3] == ("/v8/finance/chart/NVDA", "60m")
    with pytest.raises(MarketDataError):
        yahoo.fetch_candles("NVDA", "2h", 10)


def test_multi_market_routes_each_symbol_to_its_source():
    class Source:
        def __init__(self, name):
            self.name = name

        def fetch_candles(self, symbol, timeframe, limit=500):
            return self.name

    multi = MultiMarketData(Source("binance"), Source("yahoo"))  # type: ignore[arg-type]
    assert multi.fetch_candles("BTCUSDT", "1h") == "binance"
    assert multi.fetch_candles("NVDA", "1h") == "yahoo" and multi.fetch_candles("EURUSD", "1h") == "yahoo"
    assert multi.fetch_candles("^VIX", "1d") == "yahoo"
    assert multi.fetch_candles("DOGEUSDT", "1h") == "binance"


def test_fx_rates():
    class FakeYahoo:
        calls = 0

        def fetch_candles(self, symbol, timeframe, limit=500):
            FakeYahoo.calls += 1
            price = 150.0 if symbol == "JPY=X" else 1.10
            return [Candle(symbol, "1d", datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC),
                           price, price, price, price, 0, closed=True)]

    fx = FxRates(FakeYahoo())  # type: ignore[arg-type]
    assert fx.rate("USDT") == 1.0 and fx.rate("usd") == 1.0
    assert fx.rate("EUR") == pytest.approx(1.10)
    assert fx.rate("JPY") == pytest.approx(1 / 150)  # USD/JPY est inversé
    fx.rate("EUR")
    assert FakeYahoo.calls == 2  # mis en cache
    assert FxRates(static={"EUR": 1.2}).rate("EUR") == 1.2
    with pytest.raises(MarketDataError):
        FxRates().rate("EUR")


# --- Horaires des marchés ---------------------------------------------------------------------

def test_euronext_hours_and_holidays():
    assert euronext_open(datetime(2026, 9, 28, 8, 0, tzinfo=UTC))  # 10h à Paris
    assert not euronext_open(datetime(2026, 9, 28, 15, 45, tzinfo=UTC))  # 17h45 à Paris
    assert not euronext_open(datetime(2026, 9, 26, 10, 0, tzinfo=UTC))  # samedi
    assert not euronext_open(datetime(2026, 12, 25, 10, 0, tzinfo=UTC))  # Noël


def test_forex_week_runs_sunday_to_friday_new_york_time():
    assert not forex_open(datetime(2026, 9, 27, 20, 0, tzinfo=UTC))  # dimanche 16h New York
    assert forex_open(datetime(2026, 9, 27, 22, 0, tzinfo=UTC))  # dimanche 18h New York
    assert forex_open(datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    assert not forex_open(datetime(2026, 10, 2, 21, 30, tzinfo=UTC))  # vendredi 17h30 New York
    assert not forex_open(datetime(2026, 10, 3, 12, 0, tzinfo=UTC))  # samedi


def test_each_instrument_follows_its_own_calendar():
    saturday = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    monday = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    assert market_open("24/7", saturday) and not market_open("nyse", saturday)
    assert all(market_open(instrument(s).calendar, monday) for s in ("NVDA", "SPY", "CAC40", "EURUSD", "BTCUSDT"))
    block = entry_window_block("CAC40", saturday, trade_weekends=True, no_trade_hours_utc=frozenset())
    assert block and "fermé" in block
    assert entry_window_block("EURUSD", monday, trade_weekends=False, no_trade_hours_utc=frozenset()) is None


def test_universe_covers_requested_markets():
    for symbol in ("NVDA", "TSLA", "NFLX", "SPY", "CAC40", "EURUSD", "BTCUSDT"):
        assert symbol in UNIVERSE and UNIVERSE[symbol].drivers
    assert UNIVERSE["CAC40"].currency == "EUR" and UNIVERSE["USDJPY"].currency == "JPY"
    assert instrument("DOGEUSDT").asset_class == "crypto"


# --- Coûts par marché et conversion de devise ---------------------------------------------------

def test_specs_use_real_costs_per_market_only_when_enabled():
    limits = zero_cost_limits()
    specs = MarketSpecs(limits, fx=lambda currency: {"EUR": 1.1}.get(currency, 1.0), use_universe=True)
    assert specs.costs("NVDA").qty_step == 1.0 and specs.costs("EURUSD").min_qty == 1000.0
    assert specs.costs("BTCUSDT").fee_rate == 0.001
    assert specs.fx_rate("CAC40") == 1.1 and specs.fx_rate("NVDA") == 1.0
    plain = MarketSpecs(limits)
    assert plain.costs("NVDA").fee_rate == 0.0 and plain.fx_rate("CAC40") == 1.0


def test_position_size_is_computed_in_account_currency():
    """ETF CAC 40 coté en EUR : le risque de 1 % (100 USD) est converti, la taille baisse d'autant."""
    limits = zero_cost_limits(max_position_pct=1.0)
    now = datetime(2026, 9, 28, 9, tzinfo=UTC)

    def size(eur_usd):
        specs = MarketSpecs(limits, fx=lambda currency: eur_usd if currency == "EUR" else 1.0, use_universe=True)
        return RiskManager(limits, ExitParams(), specs=specs).evaluate(
            Signal("CAC40", Direction.LONG, 80.0, 1.0, now, "test", "fx"),
            AccountSnapshot(10_000.0, 10_000.0, 0), stop_loss=78.0, take_profit=84.0)

    same, converted = size(1.0), size(1.25)
    assert same.approved and converted.approved
    assert converted.quantity == pytest.approx(same.quantity / 1.25, abs=1.0)  # pas de 1 part
    assert same.risk_amount <= 100.0 + 1e-9 and converted.risk_amount <= 100.0 + 1e-9
    assert converted.risk_amount > 95.0


# --- Indicateurs de volume et structure -----------------------------------------------------------

def test_volume_indicators():
    close = np.linspace(100, 120, 40)
    volume = np.full(40, 1_000.0)
    assert mfi(close + 1, close - 1, close, volume)[-1] == 100.0  # que des flux acheteurs
    assert np.all(np.diff(obv(close, volume)[1:]) > 0)
    falling = close[::-1]
    assert mfi(falling + 1, falling - 1, falling, volume)[-1] == 0.0


def test_rsi_divergence_and_zigzag_structure():
    # Deux creux : le second plus bas en prix, mais avec un RSI plus haut => divergence haussière.
    prices = np.array([110, 108, 105, 100, 104, 107, 109, 106, 103, 98, 102, 105, 108, 110, 111], dtype=float)
    osc = np.array([60, 50, 40, 25, 45, 55, 58, 50, 42, 35, 48, 55, 60, 62, 63], dtype=float)
    assert rsi_divergence(prices, osc, lookback=15) == "haussière"
    assert rsi_divergence(prices[::-1] * -1 + 300, 100 - osc[::-1], lookback=15) in {"baissière", None}
    wave = 100 + 10 * np.sin(np.linspace(0, 6 * np.pi, 120))
    points = zigzag(wave + 0.5, wave - 0.5, lookback=5)
    kinds = [kind for _, _, kind in points]
    assert len(points) >= 5 and all(a != b for a, b in zip(kinds, kinds[1:], strict=False))
    assert not np.isnan(rsi(wave, 14)[-1])


# --- Capital de base ---------------------------------------------------------------------------------

def test_capital_can_be_raised_or_lowered_like_a_deposit():
    portfolio = Portfolio(10_000.0)
    assert portfolio.adjust_capital(15_000.0) == 5_000.0
    assert portfolio.balance == 15_000.0 and portfolio.initial_capital == 15_000.0
    assert portfolio.drawdown() == 0.0  # un dépôt n'est pas un gain, un retrait n'est pas une perte
    assert portfolio.adjust_capital(5_000.0) == -10_000.0
    assert portfolio.drawdown() == 0.0
    with pytest.raises(ValueError):
        portfolio.adjust_capital(0)
