import numpy as np
import pandas as pd
import pytest

from emcure_tracker.indicators import compute_all
from emcure_tracker.forecast import compute_forecast


def _make_df(n: int = 80, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1500.0 + np.cumsum(rng.normal(0, 8, n))
    high = close + rng.uniform(3, 12, n)
    low = close - rng.uniform(3, 12, n)
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n),
        "open": close - rng.uniform(0, 4, n),
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(200_000, 800_000, n),
    })


def test_compute_forecast_produces_result():
    df = _make_df()
    indicators = compute_all(df)
    assert indicators is not None
    result = compute_forecast(df, indicators, sentiment_score=0.1, vol_ratio=1.2)
    assert result is not None
    assert result.low <= result.mid <= result.high
    assert result.signal in {"Strong Buy", "Buy", "Hold / Wait", "Sell", "Strong Sell"}
    assert result.regime in {"trending", "ranging", "reverting", "unknown"}


def test_forecast_bias_direction():
    df = _make_df()
    indicators = compute_all(df)
    assert indicators is not None
    # Strongly positive sentiment should push bias up
    pos = compute_forecast(df, indicators, sentiment_score=0.9, vol_ratio=1.0)
    neg = compute_forecast(df, indicators, sentiment_score=-0.9, vol_ratio=1.0)
    assert pos is not None and neg is not None
    assert pos.bias_pct > neg.bias_pct


def test_forecast_conviction_scales_with_volume():
    df = _make_df()
    indicators = compute_all(df)
    assert indicators is not None
    low_vol = compute_forecast(df, indicators, sentiment_score=0.2, vol_ratio=0.5)
    high_vol = compute_forecast(df, indicators, sentiment_score=0.2, vol_ratio=2.5)
    assert low_vol is not None and high_vol is not None
    assert high_vol.conviction >= low_vol.conviction
