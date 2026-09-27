import pytest

from app.core.types import Direction
from app.strategy import CandleWindow, EmaRsiParams, EmaRsiStrategy
from tests.conftest import series_from_closes


def signals_over(candles, strategy):
    found = []
    for i in range(len(candles)):
        analysis = strategy.analyze(CandleWindow.from_candles(candles[: i + 1]))
        if analysis.signal is not None:
            found.append((i, analysis.signal))
    return found


@pytest.fixture
def trend_candles():
    flat = [100.0] * 200
    up = [100.0 + 0.5 * (i + 1) for i in range(60)]
    down = [up[-1] - 0.5 * (i + 1) for i in range(120)]
    return series_from_closes(flat + up + down)


def test_long_signal_on_transition(trend_candles):
    found = signals_over(trend_candles, EmaRsiStrategy())
    longs = [(i, s) for i, s in found if s.direction is Direction.LONG]
    assert [i for i, _ in longs] == [200]  # une seule fois, à la première bougie haussière
    signal = longs[0][1]
    assert signal.price == pytest.approx(100.5)
    assert signal.atr > 0 and "RSI" in signal.reason
    assert signal.timestamp == trend_candles[200].close_time


def test_short_signal_once_in_downtrend(trend_candles):
    shorts = [i for i, s in signals_over(trend_candles, EmaRsiStrategy()) if s.direction is Direction.SHORT]
    assert len(shorts) == 1 and shorts[0] > 260


def test_no_signal_in_flat_market_or_during_warmup(trend_candles):
    strategy = EmaRsiStrategy()
    assert signals_over(trend_candles[:200], strategy) == []
    short_window = CandleWindow.from_candles(trend_candles[:50])
    assert strategy.analyze(short_window).signal is None


def test_params_validation():
    with pytest.raises(ValueError):
        EmaRsiParams(ema_fast=50, ema_slow=20)
    assert EmaRsiStrategy(EmaRsiParams(ema_fast=10, ema_slow=30)).warmup_bars == 92
