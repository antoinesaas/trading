"""Catalogue des marchés tradables : crypto, actions, ETF et forex.

Chaque instrument définit sa source de données, sa devise, ses horaires, ses coûts de
transaction réalistes, son pas de quantité et son « milieu d'influence » (indices, taux,
dollar, volatilité, secteur) transmis à Claude avec chaque décision.

Pour ajouter un marché : ajouter une entrée à ``UNIVERSE`` puis son symbole dans ``SYMBOLS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AssetClass = Literal["crypto", "stock", "etf", "forex"]
Calendar = Literal["24/7", "nyse", "euronext", "forex"]


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    name: str
    asset_class: AssetClass
    provider: Literal["binance", "yahoo"]
    provider_symbol: str
    currency: str
    calendar: Calendar
    fee_rate: float
    slippage_rate: float
    qty_step: float
    min_qty: float
    min_notional: float
    drivers: tuple[str, ...] = ()
    tradingview: str = ""


# Milieux d'influence (symboles Yahoo) : lus pour le contexte, jamais tradés directement.
DRIVER_NAMES: dict[str, str] = {
    "^GSPC": "S&P 500", "^NDX": "Nasdaq 100", "^VIX": "VIX (volatilité US)", "^TNX": "Taux US 10 ans",
    "DX-Y.NYB": "Dollar index (DXY)", "SMH": "Semi-conducteurs (SMH)", "XLY": "Consommation discrétionnaire",
    "XLC": "Communication (XLC)", "XLK": "Technologie (XLK)", "^FCHI": "CAC 40", "^STOXX50E": "Euro Stoxx 50",
    "EURUSD=X": "EUR/USD", "GC=F": "Or", "CL=F": "Pétrole WTI", "^N225": "Nikkei 225", "BTC-USD": "Bitcoin",
}

_US_EQUITY = dict(provider="yahoo", currency="USD", calendar="nyse", fee_rate=0.0005, slippage_rate=0.0002,
                  qty_step=1.0, min_qty=1.0, min_notional=1.0)
_FOREX = dict(asset_class="forex", provider="yahoo", calendar="forex", fee_rate=0.00005, slippage_rate=0.00005,
              qty_step=1000.0, min_qty=1000.0, min_notional=1.0)
_CRYPTO = dict(asset_class="crypto", provider="binance", currency="USDT", calendar="24/7", fee_rate=0.001,
               slippage_rate=0.0005, min_notional=10.0,
               drivers=("^NDX", "DX-Y.NYB", "^VIX", "GC=F"))

UNIVERSE: dict[str, Instrument] = {i.symbol: i for i in (
    Instrument("BTCUSDT", "Bitcoin", provider_symbol="BTCUSDT", qty_step=0.00001, min_qty=0.00001,
               tradingview="BINANCE:BTCUSDT", **_CRYPTO),
    Instrument("ETHUSDT", "Ethereum", provider_symbol="ETHUSDT", qty_step=0.0001, min_qty=0.0001,
               tradingview="BINANCE:ETHUSDT", **_CRYPTO),
    Instrument("SOLUSDT", "Solana", provider_symbol="SOLUSDT", qty_step=0.001, min_qty=0.001,
               tradingview="BINANCE:SOLUSDT", **_CRYPTO),
    Instrument("NVDA", "NVIDIA", "stock", provider_symbol="NVDA", drivers=("^NDX", "SMH", "^VIX", "^TNX"),
               tradingview="NASDAQ:NVDA", **_US_EQUITY),
    Instrument("TSLA", "Tesla", "stock", provider_symbol="TSLA", drivers=("^NDX", "XLY", "^VIX", "^TNX"),
               tradingview="NASDAQ:TSLA", **_US_EQUITY),
    Instrument("NFLX", "Netflix", "stock", provider_symbol="NFLX", drivers=("^NDX", "XLC", "^VIX", "^TNX"),
               tradingview="NASDAQ:NFLX", **_US_EQUITY),
    Instrument("AAPL", "Apple", "stock", provider_symbol="AAPL", drivers=("^NDX", "XLK", "^VIX", "^TNX"),
               tradingview="NASDAQ:AAPL", **_US_EQUITY),
    Instrument("MSFT", "Microsoft", "stock", provider_symbol="MSFT", drivers=("^NDX", "XLK", "^VIX", "^TNX"),
               tradingview="NASDAQ:MSFT", **_US_EQUITY),
    Instrument("SPY", "ETF S&P 500 (SPDR)", "etf", provider_symbol="SPY",
               drivers=("^GSPC", "^VIX", "^TNX", "DX-Y.NYB"), tradingview="AMEX:SPY", **_US_EQUITY),
    Instrument("QQQ", "ETF Nasdaq 100 (Invesco)", "etf", provider_symbol="QQQ",
               drivers=("^NDX", "^VIX", "^TNX", "SMH"), tradingview="NASDAQ:QQQ", **_US_EQUITY),
    Instrument("CAC40", "ETF CAC 40 (Amundi)", "etf", "yahoo", "CAC.PA", "EUR", "euronext", fee_rate=0.0005,
               slippage_rate=0.0003, qty_step=1.0, min_qty=1.0, min_notional=1.0,
               drivers=("^FCHI", "^STOXX50E", "EURUSD=X", "^GSPC"), tradingview="EURONEXT:CAC"),
    Instrument("EURUSD", "Euro / Dollar", provider_symbol="EURUSD=X", currency="USD",
               drivers=("DX-Y.NYB", "^TNX", "^STOXX50E"), tradingview="FX:EURUSD", **_FOREX),
    Instrument("GBPUSD", "Livre / Dollar", provider_symbol="GBPUSD=X", currency="USD",
               drivers=("DX-Y.NYB", "^TNX"), tradingview="FX:GBPUSD", **_FOREX),
    Instrument("USDJPY", "Dollar / Yen", provider_symbol="JPY=X", currency="JPY",
               drivers=("DX-Y.NYB", "^TNX", "^N225"), tradingview="FX:USDJPY", **_FOREX),
)}


def instrument(symbol: str) -> Instrument:
    """Instrument du catalogue, ou instrument crypto générique (paire Binance) sinon."""
    if symbol in UNIVERSE:
        return UNIVERSE[symbol]
    return Instrument(symbol, symbol, provider_symbol=symbol, qty_step=0.0001, min_qty=0.0001,
                      tradingview=f"BINANCE:{symbol}", **_CRYPTO)
