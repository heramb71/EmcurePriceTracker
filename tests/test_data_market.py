import datetime

import pandas as pd

from emcure_tracker.data import market as mkt


def _fake_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=5),
        "open":   [100.0, 101.0, 102.0, 103.0, 104.0],
        "high":   [105.0, 106.0, 107.0, 108.0, 109.0],
        "low":    [ 98.0,  99.0, 100.0, 101.0, 102.0],
        "close":  [102.0, 103.0, 104.0, 105.0, 106.0],
        "volume": [100_000, 110_000, 90_000, 120_000, 105_000],
    })


def test_quote_from_cache_uses_last_row():
    mkt._ohlcv_cache = _fake_df()
    mkt._ohlcv_cache_date = datetime.date.today()
    quote = mkt._quote_from_cache()
    assert quote is not None
    assert quote.price == 106.0
    assert quote.prev_close == 105.0


def test_quote_from_cache_returns_none_without_cache():
    mkt._ohlcv_cache = None
    result = mkt._quote_from_cache()
    assert result is None


def test_ohlcv_returns_cached_on_same_day():
    df = _fake_df()
    mkt._ohlcv_cache = df
    mkt._ohlcv_cache_date = datetime.date.today()
    result = mkt.fetch_ohlcv()
    assert result is not None
    assert len(result.df) == 5
