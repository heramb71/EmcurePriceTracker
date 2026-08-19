"""previous_session_row — the most recent row that is NOT `today`, used
anywhere a calculation must anchor to the prior finished session (pivots,
gap-from-prev-close) regardless of whether today's row has appeared in the
fetched daily frame yet."""
from datetime import date

import pandas as pd

from src.shared.data import previous_session_row


def _df(dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame({
        "date":  pd.to_datetime(dates),
        "open":  [100.0 + i for i in range(len(dates))],
        "high":  [101.0 + i for i in range(len(dates))],
        "low":   [99.0 + i for i in range(len(dates))],
        "close": [100.5 + i for i in range(len(dates))],
    })


def test_steps_back_one_when_todays_row_is_present():
    df = _df(["2026-08-10", "2026-08-11", "2026-08-12"])
    prev = previous_session_row(df, today=date(2026, 8, 12))
    assert pd.Timestamp(prev["date"]).date() == date(2026, 8, 11)


def test_returns_last_row_when_todays_row_has_not_appeared_yet():
    # Pre-market: fetch_daily's dropna has stripped today's placeholder, so
    # the last row already IS yesterday — must not reach one day further back.
    df = _df(["2026-08-10", "2026-08-11"])
    prev = previous_session_row(df, today=date(2026, 8, 12))
    assert pd.Timestamp(prev["date"]).date() == date(2026, 8, 11)


def test_single_row_frame_returns_that_row():
    df = _df(["2026-08-12"])
    prev = previous_session_row(df, today=date(2026, 8, 12))
    assert pd.Timestamp(prev["date"]).date() == date(2026, 8, 12)
