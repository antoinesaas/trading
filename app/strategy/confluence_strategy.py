"""Scanner de confluence multi-timeframe (score de 0 à 100).

Chaque bougie clôturée reçoit un score LONG et un score SHORT :

| Critère (LONG ; SHORT symétrique)                                   | Points |
|---------------------------------------------------------------------|--------|
| EMA rapide > EMA lente                                              | 15     |
| Clôture > EMA de tendance                                           | 10     |
| Tendance du timeframe supérieur (EMA 10 > EMA 21, rééchantillonné)  | 15     |
| RSI entre 50 et 70 (momentum sans surachat)                         | 10     |
| Histogramme MACD > 0, et en hausse                                  | 5 + 5  |
| ADX >= seuil (tendance réelle, pas de range)                         | 10     |
| Prix proche de l'EMA rapide (entrée sur repli, meilleur ratio)      | 10     |
| Volume au-dessus de sa moyenne                                       | 5      |
| Clôture dans les bandes de Bollinger                                 | 5      |
| Bougie de confirmation (clôture > ouverture)                         | 5      |
| Pas de sur-extension (< 3 ATR de l'EMA lente)                        | 5      |

En range (ADX sous ``adx_min``), un second score cherche les retours à la moyenne : bande
de Bollinger touchée, RSI extrême qui se retourne, bougie de rejet, timeframe supérieur non
opposé. Un signal est émis quand le meilleur score franchit ``score_threshold`` à la
hausse. En mode IA, ce signal ne déclenche pas de trade : il demande à Claude d'analyser
la situation.
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from app.core.types import Direction, Signal
from app.market.indicators import FloatArray, adx, atr, bollinger, ema, macd, rsi, zscore
from app.strategy.base import Analysis, CandleWindow, Strategy, StrategyParams

HTF_FAST, HTF_SLOW = 10, 21


class ConfluenceParams(StrategyParams):
    ema_fast: int = Field(20, ge=2, le=200)
    ema_slow: int = Field(50, ge=3, le=300)
    ema_trend: int = Field(100, ge=10, le=300)
    htf_factor: int = Field(4, ge=2, le=24)
    rsi_length: int = Field(14, ge=2, le=100)
    atr_length: int = Field(14, ge=2, le=100)
    adx_length: int = Field(14, ge=2, le=100)
    adx_min: float = Field(18.0, ge=0, le=60)
    pullback_atr: float = Field(1.0, gt=0, le=10)
    score_threshold: float = Field(60.0, ge=10, le=100)
    setup_mode: Literal["trend", "mean_reversion", "both"] = "both"

    @model_validator(mode="after")
    def _check(self) -> ConfluenceParams:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast doit être < ema_slow")
        return self


class ConfluenceStrategy(Strategy):
    name: ClassVar[str] = "confluence"
    params_model: ClassVar[type[StrategyParams]] = ConfluenceParams
    search_space: ClassVar[dict[str, tuple[float, float]]] = {
        "ema_fast": (8, 40),
        "ema_slow": (30, 120),
        "ema_trend": (50, 200),
        "htf_factor": (2, 8),
        "adx_min": (10, 35),
        "pullback_atr": (0.3, 2.5),
        "score_threshold": (45, 85),
    }
    params: ConfluenceParams

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(3 * max(p.ema_trend, p.ema_slow, p.rsi_length + 1, 2 * p.adx_length),
                   3 * HTF_SLOW * p.htf_factor) + 2

    def indicator_series(self, window: CandleWindow) -> dict[str, FloatArray]:
        p = self.params
        return {
            f"ema{p.ema_fast}": ema(window.close, p.ema_fast),
            f"ema{p.ema_slow}": ema(window.close, p.ema_slow),
        }

    def analyze(self, window: CandleWindow) -> Analysis:
        p = self.params
        atr_values = atr(window.high, window.low, window.close, p.atr_length)
        latest_atr = float(atr_values[-1]) if len(window) and math.isfinite(atr_values[-1]) else None
        if len(window) < max(self.warmup_bars, 3) or latest_atr is None:
            return Analysis(None, latest_atr)
        series = self._series(window, atr_values)
        now, prev = self._setups(series, -1), self._setups(series, -2)
        indicators = {f"score_{kind}_{d.value.lower()}": score for (kind, d), (score, _) in now.items()}
        indicators["atr"] = latest_atr
        indicators["adx"] = float(series["adx"][-1])
        best = max(now.items(), key=lambda item: item[1][0])
        (kind, direction), (score, reasons) = best
        if score >= p.score_threshold > prev[(kind, direction)][0]:
            label = "Tendance" if kind == "trend" else "Retour à la moyenne"
            signal = Signal(
                symbol=window.symbol, direction=direction, price=float(window.close[-1]),
                atr=latest_atr, timestamp=window.close_time[-1], strategy=self.name,
                reason=f"{label} — score {score:.0f}/100 : " + ", ".join(reasons),
                indicators=indicators | {"setup_mean_reversion": float(kind == "mean_reversion")},
            )
            return Analysis(signal, latest_atr, indicators)
        return Analysis(None, latest_atr, indicators)

    def _series(self, window: CandleWindow, atr_values: FloatArray) -> dict[str, FloatArray]:
        """Indicateurs calculés une seule fois (tous causaux : la valeur en -2 est celle d'hier)."""
        p = self.params
        close = window.close
        _, upper, lower = bollinger(close)
        return {
            "close": close, "open": window.open, "high": window.high, "low": window.low,
            "fast": ema(close, p.ema_fast), "slow": ema(close, p.ema_slow), "trend": ema(close, p.ema_trend),
            "rsi": rsi(close, p.rsi_length), "hist": macd(close)[2],
            "adx": adx(window.high, window.low, close, p.adx_length), "upper": upper, "lower": lower,
            "volume_z": zscore(window.volume), "atr": atr_values,
        }

    def _setups(self, x: dict[str, FloatArray], at: int) -> dict[tuple[str, Direction], tuple[float, list[str]]]:
        p = self.params
        end = x["close"].size + at + 1
        htf = x["close"][(end - 1) % p.htf_factor:end:p.htf_factor]
        htf_bias = float(ema(htf, HTF_FAST)[-1] - ema(htf, HTF_SLOW)[-1])
        values = {k: float(v[at]) for k, v in x.items()}
        values["prev_hist"], values["prev_rsi"] = float(x["hist"][at - 1]), float(x["rsi"][at - 1])
        if not all(math.isfinite(v) for k, v in values.items() if k != "volume_z") or not math.isfinite(htf_bias):
            return {(k, d): (0.0, []) for k in ("trend", "mean_reversion")
                    for d in (Direction.LONG, Direction.SHORT)}
        result = {}
        for d in (Direction.LONG, Direction.SHORT):
            if p.setup_mode in ("trend", "both"):
                result[("trend", d)] = self._trend_score(d, values, htf_bias)
            if p.setup_mode in ("mean_reversion", "both"):
                result[("mean_reversion", d)] = self._reversion_score(d, values, htf_bias)
        return result

    def _trend_score(self, d: Direction, v: dict[str, float], htf_bias: float) -> tuple[float, list[str]]:
        s, c, a = d.sign, v["close"], v["atr"]
        rsi_ok = 50 < v["rsi"] < 70 if d is Direction.LONG else 30 < v["rsi"] < 50
        return _tally([
            (15, s * (v["fast"] - v["slow"]) > 0, "EMA alignées"),
            (10, s * (c - v["trend"]) > 0, "du bon côté de la tendance longue"),
            (15, s * htf_bias > 0, "timeframe supérieur aligné"),
            (10, rsi_ok, f"RSI {v['rsi']:.0f}"),
            (5, s * v["hist"] > 0, "MACD"),
            (5, s * (v["hist"] - v["prev_hist"]) > 0, "MACD en accélération"),
            (10, v["adx"] >= self.params.adx_min, f"ADX {v['adx']:.0f}"),
            (10, abs(c - v["fast"]) <= self.params.pullback_atr * a, "entrée sur repli"),
            (5, math.isfinite(v["volume_z"]) and v["volume_z"] > 0, "volume en hausse"),
            (5, v["lower"] < c < v["upper"], "dans les bandes de Bollinger"),
            (5, s * (c - v["open"]) > 0, "bougie de confirmation"),
            (5, abs(c - v["slow"]) <= 3 * a, "pas de sur-extension"),
        ])

    def _reversion_score(self, d: Direction, v: dict[str, float], htf_bias: float) -> tuple[float, list[str]]:
        """Range (ADX faible) : achat du bas de bande / vente du haut de bande après rejet."""
        long = d is Direction.LONG
        band, extreme = (v["lower"], v["low"]) if long else (v["upper"], v["high"])
        stretched = extreme <= band if long else extreme >= band
        rsi_extreme = v["rsi"] < 35 if long else v["rsi"] > 65
        rsi_turn = (v["rsi"] > v["prev_rsi"]) if long else (v["rsi"] < v["prev_rsi"])
        return _tally([
            (20, v["adx"] < self.params.adx_min, f"range (ADX {v['adx']:.0f})"),
            (20, stretched, "bande de Bollinger touchée"),
            (15, rsi_extreme, f"RSI extrême {v['rsi']:.0f}"),
            (5, rsi_turn, "RSI qui se retourne"),
            (15, d.sign * (v["close"] - v["open"]) > 0, "bougie de rejet"),
            (10, (v["close"] > band) if long else (v["close"] < band), "clôture réintégrée dans les bandes"),
            (5, math.isfinite(v["volume_z"]) and v["volume_z"] > 0, "volume de capitulation"),
            (10, d.sign * htf_bias >= 0, "timeframe supérieur non opposé"),
        ])


def _tally(checks: list[tuple[int, bool, str]]) -> tuple[float, list[str]]:
    score = sum(points for points, ok, _ in checks if ok)
    return float(score), [label for points, ok, label in checks if ok and points >= 10]
