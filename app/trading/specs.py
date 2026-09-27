"""Caractéristiques de marché par symbole : coûts, pas de quantité et conversion de devise.

Les montants du compte (balance, P&L, frais, risque) sont exprimés dans la devise du
compte (USD). Pour un instrument coté dans une autre devise (ETF CAC 40 en EUR, USD/JPY en
JPY), le taux de conversion est figé à l'entrée de la position (hypothèse : l'effet de
change pendant le trade est négligé).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.market.universe import UNIVERSE, instrument
from app.risk.risk_manager import RiskLimits


@dataclass(frozen=True, slots=True)
class SymbolCosts:
    fee_rate: float
    slippage_rate: float
    qty_step: float
    min_qty: float
    min_notional: float


class MarketSpecs:
    def __init__(self, limits: RiskLimits, *, fx: Callable[[str], float] | None = None,
                 use_universe: bool = False) -> None:
        self.limits = limits
        self._fx = fx
        self._use_universe = use_universe

    def _known(self, symbol: str) -> bool:
        return self._use_universe and symbol in UNIVERSE

    def costs(self, symbol: str) -> SymbolCosts:
        if self._known(symbol):
            i = UNIVERSE[symbol]
            return SymbolCosts(i.fee_rate, i.slippage_rate, i.qty_step, i.min_qty, i.min_notional)
        lim = self.limits
        return SymbolCosts(lim.fee_rate, lim.slippage_rate, lim.qty_step, lim.min_qty, lim.min_notional)

    def fx_rate(self, symbol: str) -> float:
        """Taux devise de cotation -> devise du compte (1 si inconnu ou sans convertisseur)."""
        if not self._known(symbol) or self._fx is None:
            return 1.0
        return self._fx(instrument(symbol).currency)
