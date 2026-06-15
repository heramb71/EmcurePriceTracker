"""Tests for the corporate-event (earnings) guard."""
from __future__ import annotations

from datetime import date

import src.events as events
from src.events import is_near_event


def setup_function():
    events._cache.clear()


def test_blocks_within_window(monkeypatch):
    monkeypatch.setattr(
        events, "_upcoming_earnings_dates", lambda t: [date(2026, 6, 16)]
    )
    assert is_near_event("EMCURE", today=date(2026, 6, 15), window_days=2) is True


def test_allows_outside_window(monkeypatch):
    monkeypatch.setattr(
        events, "_upcoming_earnings_dates", lambda t: [date(2026, 6, 30)]
    )
    assert is_near_event("EMCURE", today=date(2026, 6, 15), window_days=2) is False


def test_fails_open_on_no_data(monkeypatch):
    monkeypatch.setattr(events, "_upcoming_earnings_dates", lambda t: [])
    assert is_near_event("EMCURE", today=date(2026, 6, 15)) is False


def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def _fake(ticker):
        calls["n"] += 1
        return [date(2026, 6, 16)]

    monkeypatch.setattr(events, "_upcoming_earnings_dates", _fake)
    is_near_event("EMCURE", today=date(2026, 6, 15))
    is_near_event("EMCURE", today=date(2026, 6, 15))
    assert calls["n"] == 1  # second call served from cache
