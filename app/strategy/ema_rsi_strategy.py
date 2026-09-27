"""Stratégie initiale EMA 20 / EMA 50 / RSI 14 / ATR 14.

LONG  : EMA rapide > EMA lente, clôture > EMA rapide, RSI > seuil long.
SHORT : EMA rapide < EMA lente, clôture < EMA rapide, RSI < seuil short.

Le signal est émis uniquement sur une bougie CLÔTURÉE et uniquement au moment où
les conditions deviennent vraies (transition faux -> vrai), pour éviter de
ré-entrer à chaque bougie tant que les conditions restent remplies. Le Pine Script
``pine/tradingview_strategy.pine`` applique exactement la même règle.
"""

from __future__ import annotations

import math
from typing import ClassVar

from pydantic import Field, model_validator

from app.core.types import Direction, Signal
from app.market.indicators import FloatArray, atr, ema, rsi
from app.strategy.base import Analysis, CandleWindow, Strategy, StrategyParams


class EmaRsiParams(StrategyParams):
    ema_fast: int = Field(20, ge=2, le=500)
    ema_slow: int = Field(50, ge=3, le=500)
    rsi_length: int = Field(14, ge=2, le=100)
    rsi_long_threshold: float = Field(50.0, gt=0, lt=100)
    rsi_short_threshold: float = Field(50.0, gt=0, lt=100)
    atr_length: int = Field(14, ge=2, le=100)

    @model_validator(mode="after")
    def _check(self) -> EmaRsiParams:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast doit être < ema_slow")
        return self


class EmaRsiStrategy(Strategy):
    name: ClassVar[str] = "ema_rsi"
    params_model: ClassVar[type[StrategyParams]] = EmaRsiParams
    search_space: ClassVar[dict[str, tuple[float, float]]] = {
        "ema_fast": (5, 60),
        "ema_slow": (20, 200),
        "rsi_length": (5, 30),
        "rsi_long_threshold": (40, 70),
        "rsi_short_threshold": (30, 60),
        "atr_length": (5, 30),
    }
    params: EmaRsiParams

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return 3 * max(p.ema_slow, p.rsi_length + 1, p.atr_length) + 2

    def indicator_series(self, window: CandleWindow) -> dict[str, FloatArray]:
        p = self.params
        return {
            f"ema{p.ema_fast}": ema(window.close, p.ema_fast),
            f"ema{p.ema_slow}": ema(window.close, p.ema_slow),
            "rsi": rsi(window.close, p.rsi_length),
            "atr": atr(window.high, window.low, window.close, p.atr_length),
        }

    def analyze(self, window: CandleWindow) -> Analysis:
        p = self.params
        series = self.indicator_series(window)
        fast, slow = series[f"ema{p.ema_fast}"], series[f"ema{p.ema_slow}"]
        rsi_values, atr_values = series["rsi"], series["atr"]
        latest_atr = _finite(atr_values[-1]) if len(window) else None
        if len(window) < max(self.warmup_bars, 2):
            return Analysis(None, latest_atr)

        now = (fast[-1], slow[-1], window.close[-1], rsi_values[-1])
        prev = (fast[-2], slow[-2], window.close[-2], rsi_values[-2])
        if not all(math.isfinite(v) for v in (*now, *prev)) or latest_atr is None:
            return Analysis(None, latest_atr)

        indicators = {"ema_fast": now[0], "ema_slow": now[1], "rsi": now[3], "atr": latest_atr}
        direction = self._transition(now, prev)
        if direction is None:
            return Analysis(None, latest_atr, indicators)
        signal = Signal(
            symbol=window.symbol,
            direction=direction,
            price=float(window.close[-1]),
            atr=latest_atr,
            timestamp=window.close_time[-1],
            strategy=self.name,
            reason=self._describe(direction, now),
            indicators=indicators,
        )
        return Analysis(signal, latest_atr, indicators)

    def _is_long(self, values: tuple[float, float, float, float]) -> bool:
        fast, slow, close, rsi_value = values
        return fast > slow and close > fast and rsi_value > self.params.rsi_long_threshold

    def _is_short(self, values: tuple[float, float, float, float]) -> bool:
        fast, slow, close, rsi_value = values
        return fast < slow and close < fast and rsi_value < self.params.rsi_short_threshold

    def _transition(self, now: tuple[float, float, float, float],
                    prev: tuple[float, float, float, float]) -> Direction | None:
        if self._is_long(now) and not self._is_long(prev):
            return Direction.LONG
        if self._is_short(now) and not self._is_short(prev):
            return Direction.SHORT
        return None

    def _describe(self, direction: Direction, values: tuple[float, float, float, float]) -> str:
        p = self.params
        fast, slow, close, rsi_value = values
        if direction is Direction.LONG:
            return (f"EMA{p.ema_fast} {fast:.2f} > EMA{p.ema_slow} {slow:.2f}, clôture {close:.2f} "
                    f"> EMA{p.ema_fast}, RSI {rsi_value:.1f} > {p.rsi_long_threshold:g}")
        return (f"EMA{p.ema_fast} {fast:.2f} < EMA{p.ema_slow} {slow:.2f}, clôture {close:.2f} "
                f"< EMA{p.ema_fast}, RSI {rsi_value:.1f} < {p.rsi_short_threshold:g}")


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None
