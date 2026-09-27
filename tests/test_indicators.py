import math

import numpy as np
import pytest

from app.market.indicators import atr, ema, rsi


def test_ema_is_seeded_with_sma_then_smoothed():
    values = ema([1, 2, 3, 4, 5, 6], 3)
    assert all(math.isnan(v) for v in values[:2])
    assert values[2] == pytest.approx(2.0)  # SMA(1, 2, 3)
    assert values[3] == pytest.approx(3.0)  # 0.5 * 4 + 0.5 * 2
    assert values[5] == pytest.approx(5.0)


def test_ema_of_constant_series_is_constant():
    assert ema([7.0] * 30, 10)[-1] == pytest.approx(7.0)


def test_rsi_extremes_and_bounds():
    assert rsi(list(range(1, 40)), 14)[-1] == pytest.approx(100.0)
    assert rsi(list(range(40, 1, -1)), 14)[-1] == pytest.approx(0.0)
    noisy = 100 + np.cumsum(np.random.default_rng(1).normal(0, 1, 500))
    values = rsi(noisy, 14)
    finite = values[~np.isnan(values)]
    assert finite.min() >= 0 and finite.max() <= 100
    assert math.isnan(values[13]) and not math.isnan(values[14])


def test_atr_of_constant_range_equals_range():
    closes = [100.0] * 30
    values = atr([101.0] * 30, [99.0] * 30, closes, 14)
    assert values[-1] == pytest.approx(2.0)


def test_invalid_length_raises():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)
