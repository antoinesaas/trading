"""Indicateurs techniques (NumPy), alignés sur les définitions TradingView.

- EMA : initialisée par la SMA des ``length`` premières valeurs.
- RSI / ATR : lissage de Wilder (RMA), comme ``ta.rsi`` et ``ta.atr`` en Pine Script.

Les valeurs non encore calculables valent ``NaN``.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

FloatArray = NDArray[np.float64]


def _as_array(values: ArrayLike) -> FloatArray:
    return np.asarray(values, dtype=np.float64)


def _check_length(length: int) -> None:
    if length < 1:
        raise ValueError("length doit être >= 1")


def _smoothed(values: FloatArray, length: int, alpha: float) -> FloatArray:
    """Moyenne exponentielle initialisée par une SMA (base commune EMA/RMA).

    Les ``NaN`` de tête (série dérivée pas encore calculable) sont ignorés.
    """
    _check_length(length)
    out = np.full(values.shape, np.nan)
    finite = np.flatnonzero(~np.isnan(values))
    start = int(finite[0]) if finite.size else values.size
    if values.size - start < length:
        return out
    seed = start + length - 1
    out[seed] = values[start:seed + 1].mean()
    for i in range(seed + 1, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def ema(values: ArrayLike, length: int) -> FloatArray:
    return _smoothed(_as_array(values), length, 2.0 / (length + 1))


def rma(values: ArrayLike, length: int) -> FloatArray:
    """Moyenne mobile de Wilder (alpha = 1/length)."""
    return _smoothed(_as_array(values), length, 1.0 / length)


def rsi(close: ArrayLike, length: int = 14) -> FloatArray:
    prices = _as_array(close)
    out = np.full(prices.shape, np.nan)
    if prices.size <= length:
        return out
    change = np.diff(prices)
    avg_gain = rma(np.clip(change, 0, None), length)
    avg_loss = rma(np.clip(-change, 0, None), length)
    with np.errstate(divide="ignore", invalid="ignore"):
        values = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    values = np.where(avg_loss == 0, 100.0, values)  # convention Pine : pas de perte -> 100
    values = np.where(np.isnan(avg_gain), np.nan, values)
    out[1:] = values
    return out


def true_range(high: ArrayLike, low: ArrayLike, close: ArrayLike) -> FloatArray:
    hi, lo, c = _as_array(high), _as_array(low), _as_array(close)
    prev_close = np.concatenate(([np.nan], c[:-1]))
    ranges = np.vstack((hi - lo, np.abs(hi - prev_close), np.abs(lo - prev_close)))
    tr = np.nanmax(ranges, axis=0)
    if tr.size:
        tr[0] = hi[0] - lo[0]
    return tr


def atr(high: ArrayLike, low: ArrayLike, close: ArrayLike, length: int = 14) -> FloatArray:
    return rma(true_range(high, low, close), length)


def sma(values: ArrayLike, length: int) -> FloatArray:
    data = _as_array(values)
    _check_length(length)
    out = np.full(data.shape, np.nan)
    if data.size >= length:
        out[length - 1:] = np.lib.stride_tricks.sliding_window_view(data, length).mean(axis=1)
    return out


def rolling_std(values: ArrayLike, length: int) -> FloatArray:
    data = _as_array(values)
    _check_length(length)
    out = np.full(data.shape, np.nan)
    if data.size >= length:
        out[length - 1:] = np.lib.stride_tricks.sliding_window_view(data, length).std(axis=1)
    return out


def macd(close: ArrayLike, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Ligne MACD, ligne de signal et histogramme."""
    prices = _as_array(close)
    line = ema(prices, fast) - ema(prices, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def bollinger(close: ArrayLike, length: int = 20,
              mult: float = 2.0) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Bandes de Bollinger : (milieu, haute, basse)."""
    mid = sma(close, length)
    width = rolling_std(close, length) * mult
    return mid, mid + width, mid - width


def adx(high: ArrayLike, low: ArrayLike, close: ArrayLike, length: int = 14) -> FloatArray:
    """Average Directional Index (Wilder) : force de la tendance, 0 à 100."""
    hi, lo = _as_array(high), _as_array(low)
    up = np.concatenate(([0.0], np.diff(hi)))
    down = np.concatenate(([0.0], -np.diff(lo)))
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_values = atr(high, low, close, length)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * rma(plus_dm, length) / atr_values
        minus_di = 100 * rma(minus_dm, length) / atr_values
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.where(np.isfinite(dx) | np.isnan(atr_values), dx, 0.0)
    return rma(dx, length)


def zscore(values: ArrayLike, length: int = 20) -> FloatArray:
    data = _as_array(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (data - sma(data, length)) / rolling_std(data, length)
    return np.where(np.isfinite(result), result, np.nan)


def swing_levels(high: ArrayLike, low: ArrayLike, lookback: int = 3,
                 count: int = 3) -> tuple[list[float], list[float]]:
    """Derniers plus bas (supports) et plus hauts (résistances) de pivot confirmés."""
    hi, lo = _as_array(high), _as_array(low)
    supports: list[float] = []
    resistances: list[float] = []
    for i in range(hi.size - lookback - 1, lookback - 1, -1):
        window = slice(i - lookback, i + lookback + 1)
        if len(resistances) < count and hi[i] == hi[window].max():
            resistances.append(float(hi[i]))
        if len(supports) < count and lo[i] == lo[window].min():
            supports.append(float(lo[i]))
        if len(supports) >= count and len(resistances) >= count:
            break
    return supports, resistances
