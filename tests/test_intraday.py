"""exclude_incomplete_today — drops an in-progress "today" daily row before
trailing-window signals (SMA7 gap, 7D trend) are computed, so live matches
run_backtest's df.iloc[:i] (prior CLOSED days only)."""
from datetime import date

import pandas as pd

from src.emcure.intraday import exclude_incomplete_today


def _df(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "date":  pd.to_datetime(dates),
        "open":  [100.0] * len(dates),
        "high":  [101.0] * len(dates),
        "low":   [99.0] * len(dates),
        "close": [100.0] * len(dates),
    })


def test_drops_last_row_when_it_is_today():
    df = _df(["2026-08-10", "2026-08-11", "2026-08-12"])
    out = exclude_incomplete_today(df, today=date(2026, 8, 12))
    assert list(out["date"].dt.date) == [date(2026, 8, 10), date(2026, 8, 11)]


def test_keeps_df_unchanged_when_last_row_is_a_prior_closed_day():
    # Pre-market call: today's row hasn't appeared in df yet.
    df = _df(["2026-08-10", "2026-08-11"])
    out = exclude_incomplete_today(df, today=date(2026, 8, 12))
    assert len(out) == 2


def test_never_drops_down_to_an_empty_frame():
    df = _df(["2026-08-12"])
    out = exclude_incomplete_today(df, today=date(2026, 8, 12))
    assert len(out) == 1
