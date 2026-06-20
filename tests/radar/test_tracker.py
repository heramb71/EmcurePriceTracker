"""Tests for outcome evaluation: window labelling + the due-sweep wiring."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.radar import store, tracker


def _bar(h, l, c):
    return {"high": h, "low": l, "close": c}


def test_window_target_before_stop_is_win():
    bars = [_bar(101, 99, 100), _bar(106, 100, 105)]  # hits target 104
    price, mfe, mae, outcome = tracker.evaluate_window(100, 95, 104, bars)
    assert outcome == "WIN"
    assert price == 105.0
    assert mfe == 6.0   # max high 106 - entry 100
    assert mae == -1.0  # min low 99 - entry 100


def test_window_stop_before_target_is_loss():
    bars = [_bar(101, 94, 96), _bar(110, 96, 108)]  # bar1 low 94 ≤ stop 95
    _, _, _, outcome = tracker.evaluate_window(100, 95, 104, bars)
    assert outcome == "LOSS"


def test_window_same_bar_tie_is_loss():
    bars = [_bar(105, 94, 100)]  # hits both stop 95 and target 104 in one bar
    _, _, _, outcome = tracker.evaluate_window(100, 95, 104, bars)
    assert outcome == "LOSS"


def test_window_neither_is_neutral():
    bars = [_bar(102, 98, 101)]
    _, _, _, outcome = tracker.evaluate_window(100, 95, 104, bars)
    assert outcome == "NEUTRAL"


def test_window_empty_is_none():
    assert tracker.evaluate_window(100, 95, 104, []) == (None, None, None, None)


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "radar.db"))
    yield c
    c.close()


def test_evaluate_due_writes_outcomes(conn):
    # Arrange: a signal old enough that 1h/4h/1d windows have matured.
    ts = datetime(2026, 6, 15, 10, 0)
    sid = store.insert_signal(
        conn, stock="EMCURE", signal_type="sma7_reversion", confidence=80,
        regime="TRENDING_BULL", price_at_alert=100.0, suggested_stop=95.0,
        suggested_target=104.0, rr=1.3, ts=ts,
    )
    now = ts + timedelta(days=2)

    # Daily bars covering the windows; price rises to target.
    daily = pd.DataFrame({
        "date": pd.date_range(ts + timedelta(days=1), periods=2, freq="D"),
        "high": [105.0, 107.0], "low": [99.0, 103.0], "close": [104.5, 106.0],
    })
    intraday = pd.DataFrame({
        "date": [ts + timedelta(minutes=30), ts + timedelta(hours=2)],
        "high": [104.5, 105.0], "low": [99.5, 100.0], "close": [104.2, 104.8],
    })

    # Act
    written = tracker.evaluate_due(
        conn, now=now,
        daily_fetch=lambda s: daily,
        intraday_fetch=lambda s: intraday,
    )

    # Assert: matured horizons recorded, all WIN.
    assert written >= 3
    rows = conn.execute(
        "SELECT horizon, outcome FROM outcomes WHERE signal_id=?", (sid,)
    ).fetchall()
    recorded = {r["horizon"]: r["outcome"] for r in rows}
    assert "1h" in recorded and "1d" in recorded
    assert all(v == "WIN" for v in recorded.values())

    # Idempotent: a second sweep writes nothing new.
    assert tracker.evaluate_due(conn, now=now,
                                daily_fetch=lambda s: daily,
                                intraday_fetch=lambda s: intraday) == 0
