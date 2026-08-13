import numpy as np
import pandas as pd

from src.shared.indicators import (
    compute_atr,
    compute_avg_volume,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
)


def _make_df(n: int = 80, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000.0 + np.cumsum(rng.normal(0, 5, n))
    high = close + rng.uniform(2, 10, n)
    low = close - rng.uniform(2, 10, n)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n),
        "open": close - rng.uniform(0, 3, n),
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(100_000, 500_000, n),
    })


def test_rsi_range():
    df = _make_df()
    rsi = compute_rsi(df["close"])
    assert 0 <= rsi <= 100


def test_macd_returns_three_floats():
    df = _make_df()
    result = compute_macd(df["close"])
    assert len(result) == 3
    assert all(isinstance(v, float) for v in result)


def test_bollinger_order():
    df = _make_df()
    upper, mid, lower = compute_bollinger(df["close"])
    assert upper > mid > lower


def test_ema_is_float():
    df = _make_df()
    val = compute_ema(df["close"], 20)
    assert isinstance(val, float)


def test_avg_volume_positive():
    df = _make_df()
    assert compute_avg_volume(df) > 0


def test_compute_atr_ignores_trailing_nan_row():
    """yfinance's pre-market all-NaN 'today' row must not zero out ATR."""
    rows = [{"high": 100 + i, "low": 90 + i, "close": 95 + i} for i in range(30)]
    rows.append({"high": np.nan, "low": np.nan, "close": np.nan})   # today's placeholder
    atr = compute_atr(pd.DataFrame(rows))
    assert atr > 0
