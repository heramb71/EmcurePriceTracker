"""Tests for the headless scheduler's wake-target logic (weekend + holiday skip).

These pin the fix for the EOD/holiday delivery bug: the wake target must skip
weekends *and* holidays, and must never resolve to a past time (which previously
turned the sleep loop into a busy-spin on holidays). Dates use 2026-06-16, a
known Tuesday.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import main_headless
from main_headless import _next_wake_target, _IST


def _ist(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_IST)


@pytest.fixture
def no_holidays(monkeypatch):
    monkeypatch.setattr(main_headless, "is_market_holiday", lambda d=None: False)


def test_before_wakeup_returns_today_0905(no_holidays):
    # Tue 08:30 → wake 10 min before today's open
    assert _next_wake_target(_ist(2026, 6, 16, 8, 30)) == _ist(2026, 6, 16, 9, 5)


def test_in_runup_returns_today_open(no_holidays):
    # Tue 09:10 (inside the 10-min run-up) → proceed straight at the 09:15 open
    assert _next_wake_target(_ist(2026, 6, 16, 9, 10)) == _ist(2026, 6, 16, 9, 15)


def test_after_close_returns_next_day(no_holidays):
    # Tue 16:00 → Wed 09:05
    assert _next_wake_target(_ist(2026, 6, 16, 16, 0)) == _ist(2026, 6, 17, 9, 5)


def test_friday_after_close_skips_weekend(no_holidays):
    # Fri 16:00 → Mon 09:05
    assert _next_wake_target(_ist(2026, 6, 19, 16, 0)) == _ist(2026, 6, 22, 9, 5)


def test_saturday_skips_to_monday(no_holidays):
    assert _next_wake_target(_ist(2026, 6, 20, 12, 0)) == _ist(2026, 6, 22, 9, 5)


def _only_jun16_holiday(d=None):
    return d == datetime(2026, 6, 16).date()


def test_holiday_today_skips_to_next_trading_day(monkeypatch):
    monkeypatch.setattr(main_headless, "is_market_holiday", _only_jun16_holiday)
    # Tue is a holiday → Wed 09:05
    assert _next_wake_target(_ist(2026, 6, 16, 10, 0)) == _ist(2026, 6, 17, 9, 5)


def test_holiday_during_runup_does_not_busy_spin(monkeypatch):
    monkeypatch.setattr(main_headless, "is_market_holiday", _only_jun16_holiday)
    # Even at 09:05 on a holiday the target must advance, never resolve to today.
    assert _next_wake_target(_ist(2026, 6, 16, 9, 5)) == _ist(2026, 6, 17, 9, 5)
