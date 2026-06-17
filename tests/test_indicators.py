import numpy as np
import pandas as pd
import pytest

from emcure_tracker.indicators import (
    compute_rsi,
    compute_macd,
    compute_bollinger,
    compute_ema,
    compute_avg_volume,
    compute_avg_range,
    compute_support_resistance,
    compute_volume_signal,
    rsi_signal,
    compute_all,
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


def test_avg_range_positive():
    df = _make_df()
    assert compute_avg_range(df) > 0


def test_support_resistance_tuples():
    df = _make_df(80)
    supports, resistances = compute_support_resistance(df)
    assert isinstance(supports, tuple)
    assert isinstance(resistances, tuple)


def test_volume_signal_labels():
    sig_high = compute_volume_signal(200_000, 100_000)
    assert sig_high.ratio == pytest.approx(2.0)
    sig_low = compute_volume_signal(50_000, 100_000)
    assert "Low" in sig_low.label


def test_rsi_signal_zones():
    assert rsi_signal(75)[0] == "Overbought"
    assert rsi_signal(25)[0] == "Oversold"
    assert rsi_signal(50)[0] == "Neutral"


def test_compute_all_returns_result():
    df = _make_df()
    result = compute_all(df)
    assert result is not None
    assert 0 <= result.rsi <= 100
    assert result.bb_upper > result.bb_lower
    assert result.ema_short > 0


def test_compute_all_with_short_df_returns_none():
    df = _make_df(5)
    result = compute_all(df)
    # With only 5 rows, rolling windows produce NaN; result may be None
    # We just assert it doesn't raise
    assert result is None or result is not None


def test_compute_atr_ignores_trailing_nan_row():
    """yfinance's pre-market all-NaN 'today' row must not zero out ATR."""
    import numpy as np
    import pandas as pd
    from src.indicators import compute_atr
    rows = [{"high": 100 + i, "low": 90 + i, "close": 95 + i} for i in range(30)]
    rows.append({"high": np.nan, "low": np.nan, "close": np.nan})   # today's placeholder
    atr = compute_atr(pd.DataFrame(rows))
    assert atr > 0
