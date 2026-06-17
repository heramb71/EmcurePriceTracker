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


# ── Legacy-alert suppression under the managed-cycle ─────────────────────────
# When MANAGED_CYCLE=true the managed-cycle emits its own aligned alerts, so the
# old intraday/Supertrend alerts must be suppressed to avoid contradicting it.

def _legacy_buy_data() -> dict:
    return {
        "intra_signal": {"action": "BUY"},
        "quote":        {"price": 1700.0, "high": 1710.0, "low": 1690.0, "change_pct": -1.0},
        "rupee_levels": {"qty": 8, "entry": 1700.0, "t1": 1710.0, "t2": 1720.0,
                         "t3": 1725.0, "sl": 1690.0, "max_risk": 80},
        "sma7_gap":     {"gap": -22, "sma7": 1722.0},
        "trend_7d":     "Upward",
        "trade_pred":   {"score": 60, "tier": "B — MODERATE", "reach_t1": 60,
                         "reach_t2": 40, "reach_t3": 20, "p_stop": 30, "ev": 100},
    }


def _run_dispatch(monkeypatch, sent: list) -> None:
    # No manual trade, WhatsApp send recorded instead of sent.
    monkeypatch.setattr(main_headless, "check_and_mark", lambda *a, **k: [])
    monkeypatch.setattr(main_headless, "send_whatsapp_alert", lambda *a, **k: sent.append(a) or True)
    now = datetime(2026, 6, 18, 11, 0, tzinfo=_IST)   # 11:00 — outside every briefing window
    main_headless._dispatch_alerts(
        "EMCURE", _legacy_buy_data(), now, {},
        "sid", "tok", "+1", "+91999", 100000.0, 4500.0, 1.0, "", "",
    )


def test_intraday_signal_fires_when_managed_cycle_off(monkeypatch):
    monkeypatch.delenv("MANAGED_CYCLE", raising=False)
    sent: list = []
    _run_dispatch(monkeypatch, sent)
    assert sent, "legacy intraday BUY alert should fire when managed-cycle is off"


def test_intraday_signal_suppressed_when_managed_cycle_on(monkeypatch):
    monkeypatch.setenv("MANAGED_CYCLE", "true")
    sent: list = []
    _run_dispatch(monkeypatch, sent)
    assert sent == [], "legacy intraday BUY alert must be suppressed under managed-cycle"
