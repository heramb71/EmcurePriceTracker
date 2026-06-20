"""Tests for the radar SQLite store: round-trip, due windows, idempotency."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.radar import store


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "radar.db"))
    yield c
    c.close()


def _insert(conn, ts: datetime, **over) -> int:
    base = dict(
        stock="EMCURE", signal_type="sma7_reversion", confidence=82,
        regime="TRENDING_BULL", price_at_alert=1400.0, suggested_stop=1380.0,
        suggested_target=1430.0, rr=1.5, ts=ts,
    )
    base.update(over)
    return store.insert_signal(conn, **base)


def test_insert_signal_roundtrip(conn):
    # Arrange / Act
    sid = _insert(conn, datetime(2026, 6, 20, 10, 0))
    row = conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()

    # Assert
    assert row["stock"] == "EMCURE"
    assert row["confidence"] == 82
    assert row["rr"] == 1.5


def test_due_outcomes_returns_only_matured(conn):
    # Arrange: a signal 2 hours old
    ts = datetime(2026, 6, 20, 10, 0)
    sid = _insert(conn, ts)
    now = ts + timedelta(hours=2)

    # Act
    due = store.due_outcomes(conn, now=now)
    horizons = {d["horizon"] for d in due if d["signal_id"] == sid}

    # Assert: only the 1h window has matured (4h/1d/... have not)
    assert horizons == {"1h"}


def test_due_outcomes_excludes_recorded(conn):
    ts = datetime(2026, 6, 20, 10, 0)
    sid = _insert(conn, ts)
    now = ts + timedelta(days=11)  # everything matured

    store.record_outcome(
        conn, signal_id=sid, horizon="1h", price=1410.0,
        mfe=10.0, mae=-2.0, outcome="WIN", evaluated_at=now,
    )

    due = store.due_outcomes(conn, now=now)
    horizons = {d["horizon"] for d in due if d["signal_id"] == sid}
    assert "1h" not in horizons
    assert horizons == {"4h", "1d", "3d", "5d", "10d"}


def test_record_outcome_is_idempotent(conn):
    ts = datetime(2026, 6, 20, 10, 0)
    sid = _insert(conn, ts)

    store.record_outcome(conn, signal_id=sid, horizon="1h", price=1410.0,
                         mfe=10.0, mae=-2.0, outcome="WIN")
    store.record_outcome(conn, signal_id=sid, horizon="1h", price=1420.0,
                         mfe=20.0, mae=-1.0, outcome="WIN")  # upsert

    rows = conn.execute(
        "SELECT * FROM outcomes WHERE signal_id=? AND horizon='1h'", (sid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["price"] == 1420.0  # latest write wins
