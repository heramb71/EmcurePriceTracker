"""Unit tests for vectorised swing indicators."""
import numpy as np
import pandas as pd

from src.swing import indicators as ind


def _frame(closes):
    n = len(closes)
    return pd.DataFrame({
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


def test_ema_converges_to_constant():
    s = pd.Series([10.0] * 30)
    assert round(ind.ema(s, 10).iloc[-1], 6) == 10.0


def test_rsi_all_gains_near_100():
    s = pd.Series(np.arange(1, 40, dtype=float))  # strictly rising
    val = ind.rsi(s, 14).iloc[-1]
    assert val > 95


def test_rsi_all_losses_near_zero():
    s = pd.Series(np.arange(40, 1, -1, dtype=float))  # strictly falling
    assert ind.rsi(s, 14).iloc[-1] < 5


def test_atr_positive_and_series():
    df = _frame(list(np.linspace(100, 120, 30)))
    a = ind.atr(df, 14)
    assert isinstance(a, pd.Series) and a.iloc[-1] > 0


def test_rvol_doubles_on_double_volume():
    df = _frame([100.0] * 30)
    df.loc[df.index[-1], "volume"] = 2000  # last bar double the average
    r = ind.rvol(df["volume"], 20).iloc[-1]
    assert 1.4 < r < 2.1  # ~ current / rolling-mean-including-self


def test_rolling_vwap_between_low_and_high():
    df = _frame(list(np.linspace(100, 110, 30)))
    v = ind.rolling_vwap(df, 20).iloc[-1]
    assert df["low"].min() <= v <= df["high"].max()
