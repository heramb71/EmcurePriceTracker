"""exclude_incomplete_today — drops today's daily row while the session is
still IN PROGRESS, before trailing-window signals (SMA7 gap, 7D trend) are
computed, so live matches run_backtest's df.iloc[:i] (prior CLOSED days
only). Once the 15:30 close has passed, today's row is a closed day and must
stay in the window (the EOD summary's forward-looking "tomorrow" preview)."""
from datetime import date, datetime

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


def _t(hh: int, mm: int) -> datetime:
    return datetime(2026, 8, 12, hh, mm)  # a Wednesday


def test_drops_last_row_when_today_is_still_mid_session(monkeypatch):
    from src.emcure import schedule
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)
    df = _df(["2026-08-10", "2026-08-11", "2026-08-12"])
    out = exclude_incomplete_today(df, now=_t(12, 0))
    assert list(out["date"].dt.date) == [date(2026, 8, 10), date(2026, 8, 11)]


def test_keeps_todays_row_once_the_close_has_passed(monkeypatch):
    # EOD window (15:30+): today's bar is final — must count in the SMA7/trend
    # window (the "tomorrow" preview would go stale by a full session otherwise).
    from src.emcure import schedule
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)
    df = _df(["2026-08-10", "2026-08-11", "2026-08-12"])
    out = exclude_incomplete_today(df, now=_t(15, 45))
    assert len(out) == 3


def test_keeps_df_unchanged_when_last_row_is_a_prior_closed_day(monkeypatch):
    # Pre-market call: today's row hasn't appeared in df yet.
    from src.emcure import schedule
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)
    df = _df(["2026-08-10", "2026-08-11"])
    out = exclude_incomplete_today(df, now=_t(9, 5))
    assert len(out) == 2


def test_never_drops_down_to_an_empty_frame(monkeypatch):
    from src.emcure import schedule
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)
    df = _df(["2026-08-12"])
    out = exclude_incomplete_today(df, now=_t(12, 0))
    assert len(out) == 1


def test_keeps_todays_row_on_a_weekend_even_if_dated_today(monkeypatch):
    # Defensive: is_market_open is already False on a non-trading day, so the
    # date-match alone must never be enough to drop the row.
    from src.emcure import schedule
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)
    df = _df(["2026-08-14", "2026-08-15"])  # Saturday
    out = exclude_incomplete_today(df, now=datetime(2026, 8, 15, 12, 0))
    assert len(out) == 2
