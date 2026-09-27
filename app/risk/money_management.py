"""Money management : combien risquer sur le prochain trade.

Le risque demandé (par la configuration ou par Claude) est plafonné puis réduit par :
1. un plafond dur par trade (``hard_max_risk_per_trade``), modulé par la confiance ;
2. un plafond de Kelly fractionné, une fois assez de trades pour l'estimer ;
3. la réduction en drawdown (jusqu'à 25 % du risque au drawdown maximal) ;
4. la réduction après une série de pertes.
Le risque total ouvert (« heat ») et le risque corrélé sont plafonnés ensuite par le
``RiskManager``. Aucune de ces règles ne peut être contournée par l'IA.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.core.types import Direction

if TYPE_CHECKING:
    from app.trading.positions import Trade


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    trades: int = 0
    win_rate: float = 0.0
    payoff_ratio: float = 0.0  # gain moyen / perte moyenne (en valeur absolue)
    consecutive_losses: int = 0
    last_loss_time: datetime | None = None

    @classmethod
    def from_trades(cls, trades: Sequence[Trade]) -> PerformanceSnapshot:
        pnls = [t.net_pnl for t in trades]
        if not pnls:
            return cls()
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p <= 0]
        streak = 0
        for pnl in reversed(pnls):
            if pnl > 0:
                break
            streak += 1
        last_loss = next((t.exit_time for t in reversed(trades) if t.net_pnl <= 0), None)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return cls(trades=len(pnls), win_rate=len(wins) / len(pnls),
                   payoff_ratio=avg_win / avg_loss if avg_loss else 0.0,
                   consecutive_losses=streak, last_loss_time=last_loss)


@dataclass(frozen=True, slots=True)
class OpenRisk:
    """Risque ouvert (perte si tous les stops étaient touchés), en fraction d'equity."""

    total: float = 0.0
    long: float = 0.0
    short: float = 0.0

    def same_direction(self, direction: Direction) -> float:
        return self.long if direction is Direction.LONG else self.short


def kelly_fraction(win_rate: float, payoff_ratio: float) -> float:
    """Fraction de Kelly : p - (1 - p) / b (négative = pas d'avantage statistique)."""
    if payoff_ratio <= 0:
        return 0.0
    return win_rate - (1 - win_rate) / payoff_ratio


def confidence_scale(confidence: float, min_confidence: float) -> float:
    """0,5 au seuil minimal de confiance, 1,0 à confiance maximale."""
    span = max(1e-9, 1 - min_confidence)
    return 0.5 + 0.5 * min(1.0, max(0.0, (confidence - min_confidence) / span))


def risk_fraction(requested: float, *, confidence: float, min_confidence: float, hard_max: float,
                  min_risk: float, stats: PerformanceSnapshot, drawdown: float, max_drawdown: float,
                  kelly_multiplier: float, kelly_min_trades: int) -> tuple[float, list[str]]:
    """Fraction de l'equity à risquer et explication de chaque ajustement."""
    notes: list[str] = []
    cap = hard_max * confidence_scale(confidence, min_confidence)
    fraction = min(requested, cap)
    if fraction < requested:
        notes.append(f"plafonné à {cap:.2%} (plafond {hard_max:.2%} x confiance {confidence:.2f})")
    if stats.trades >= kelly_min_trades:
        kelly = kelly_fraction(stats.win_rate, stats.payoff_ratio)
        kelly_cap = max(min_risk, kelly_multiplier * kelly)
        if fraction > kelly_cap:
            fraction = kelly_cap
            notes.append(f"Kelly fractionné {kelly_cap:.2%} (Kelly brut {kelly:.2%})")
    if max_drawdown > 0 and drawdown > 0:
        factor = 1 - 0.75 * min(1.0, drawdown / max_drawdown)
        fraction *= factor
        notes.append(f"drawdown {drawdown:.2%} : x{factor:.2f}")
    if stats.consecutive_losses >= 2:
        factor = 0.75 if stats.consecutive_losses == 2 else 0.5
        fraction *= factor
        notes.append(f"{stats.consecutive_losses} pertes consécutives : x{factor:.2f}")
    return fraction, notes
