"""build_snapshot's day-boundary handling:
  - sma7 must exclude today's row while the session is still forming (mid-
    session) but include it once the close has passed (self-dilution fix,
    same root cause as the EMCURE managed-cycle live incident).
  - prev_close/prev_high must resolve to yesterday's OHLC whether or not
    today's row has appeared in the fetched frame yet (pre-market vs
    mid-session/post-close).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.emcure import schedule
from src.radar import features

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _no_holidays(monkeypatch):
    monkeypatch.setattr(schedule, "is_market_holiday", lambda d: False)


def _make_df(n: int = 31, today: str = "2026-08-12") -> pd.DataFrame:
    dates = pd.bdate_range(end=today, periods=n)
    closes = [1400.0 + i for i in range(n)]  # steadily rising, so the last
    return pd.DataFrame({
        "date":   dates,
        "open":   [c - 1 for c in closes],
        "high":   [c + 2 for c in closes],
        "low":    [c - 2 for c in closes],
        "close":  closes,
        "volume": [100_000] * n,
    })


def _patch_fetch(monkeypatch, df: pd.DataFrame):
    monkeypatch.setattr(features, "fetch_daily", lambda ticker, days=120: df)
    monkeypatch.setattr(features, "fetch_intraday", lambda *a, **k: None)


def test_sma7_excludes_todays_row_mid_session(monkeypatch):
    df = _make_df()
    _patch_fetch(monkeypatch, df)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=_IST)  # mid-session, Wednesday
    snap = features.build_snapshot("EMCURE", now=now)
    assert snap is not None
    # SMA7 of the 7 CLOSED days strictly before "today" (rows -8..-2).
    expected = round(df["close"].iloc[-8:-1].mean(), 2)
    assert snap.sma7 == expected


def test_sma7_includes_todays_row_after_close(monkeypatch):
    df = _make_df()
    _patch_fetch(monkeypatch, df)
    now = datetime(2026, 8, 12, 15, 45, tzinfo=_IST)  # EOD window, after 15:30
    snap = features.build_snapshot("EMCURE", now=now)
    assert snap is not None
    # SMA7 of the 7 most recent rows, today's now-final close included.
    expected = round(df["close"].iloc[-7:].mean(), 2)
    assert snap.sma7 == expected


def test_prev_close_and_high_mid_session(monkeypatch):
    df = _make_df()
    _patch_fetch(monkeypatch, df)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=_IST)
    snap = features.build_snapshot("EMCURE", now=now)
    assert snap is not None
    assert snap.prev_close == round(float(df["close"].iloc[-2]), 2)
    assert snap.prev_high == round(float(df["high"].iloc[-2]), 2)


def test_prev_close_and_high_pre_market_when_todays_row_absent(monkeypatch):
    # Today's row hasn't appeared in the fetched frame yet — the last row IS
    # yesterday, so prev_close/prev_high must NOT reach one day further back.
    df = _make_df(today="2026-08-11")
    _patch_fetch(monkeypatch, df)
    now = datetime(2026, 8, 12, 8, 30, tzinfo=_IST)  # pre-market
    snap = features.build_snapshot("EMCURE", now=now)
    assert snap is not None
    assert snap.prev_close == round(float(df["close"].iloc[-1]), 2)
    assert snap.prev_high == round(float(df["high"].iloc[-1]), 2)
