"""Tests for the centralized scheduled-alert window predicates."""
from __future__ import annotations

from datetime import datetime

from src.emcure import schedule


def _t(hh: int, mm: int) -> datetime:
    return datetime(2026, 7, 3, hh, mm)  # a Friday


def test_pre_open_window():
    assert schedule.in_pre_open(_t(9, 0))
    assert schedule.in_pre_open(_t(9, 14))
    assert not schedule.in_pre_open(_t(9, 15))
    assert not schedule.in_pre_open(_t(8, 59))


def test_post_open_window():
    assert schedule.in_post_open(_t(9, 20))
    assert schedule.in_post_open(_t(9, 59))
    assert not schedule.in_post_open(_t(9, 19))
    assert not schedule.in_post_open(_t(10, 0))


def test_eod_window():
    assert schedule.in_eod(_t(15, 30))
    assert schedule.in_eod(_t(15, 59))
    assert not schedule.in_eod(_t(15, 29))
    assert not schedule.in_eod(_t(16, 0))


def test_windows_are_mutually_exclusive():
    # No single minute of the day falls in two windows.
    for hh in range(24):
        for mm in range(60):
            now = _t(hh, mm)
            hits = sum([schedule.in_pre_open(now), schedule.in_post_open(now),
                        schedule.in_eod(now)])
            assert hits <= 1, f"{hh:02d}:{mm:02d} in multiple windows"


def test_due_returns_kind_when_unsent():
    assert schedule.due(_t(9, 5), {}) == schedule.PRE_OPEN
    assert schedule.due(_t(9, 30), {}) == schedule.POST_OPEN
    assert schedule.due(_t(15, 40), {}) == schedule.EOD


def test_due_none_when_already_sent():
    now = _t(9, 5)
    sent = {schedule.daily_key(schedule.PRE_OPEN, now): now}
    assert schedule.due(now, sent) is None


def test_due_none_outside_windows():
    assert schedule.due(_t(11, 0), {}) is None


def test_daily_key_is_per_date():
    assert schedule.daily_key("eod", _t(15, 40)) == "eod_2026-07-03"
