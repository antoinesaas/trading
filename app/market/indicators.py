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
    """Moyenne exponentielle initialisée par une SMA (base commune EMA/RMA)."""
    _check_length(length)
    out = np.full(values.shape, np.nan)
    if values.size < length:
        return out
    out[length - 1] = values[:length].mean()
    for i in range(length, values.size):
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
