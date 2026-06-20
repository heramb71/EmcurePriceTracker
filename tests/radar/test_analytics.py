"""Tests for performance analytics: grouping, expectancy, leaders, cold-start."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.radar import analytics, store


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(str(tmp_path / "radar.db"))
    yield c
    c.close()


def _signal(conn, stock, sig, regime, entry):
    return store.insert_signal(
        conn, stock=stock, signal_type=sig, confidence=80, regime=regime,
        price_at_alert=entry, suggested_stop=entry - 5, suggested_target=entry + 5,
        rr=1.0, ts=datetime(2026, 6, 1, 10, 0),
    )


def _outcome(conn, sid, exit_price, outcome, horizon="5d"):
    store.record_outcome(conn, signal_id=sid, horizon=horizon, price=exit_price,
                         mfe=abs(exit_price), mae=-1.0, outcome=outcome)


def test_summary_expectancy_and_profit_factor(conn):
    # EMCURE: 2 wins (+10 each), 1 loss (-5) → PF = 20/5 = 4, E = 15/3 = 5.
    for entry in (100, 100):
        sid = _signal(conn, "EMCURE", "sma7_reversion", "TRENDING_BULL", entry)
        _outcome(conn, sid, entry + 10, "WIN")
    sid = _signal(conn, "EMCURE", "sma7_reversion", "TRENDING_BULL", 100)
    _outcome(conn, sid, 95, "LOSS")

    stats = analytics.summary(conn, "stock")
    emcure = next(g for g in stats if g.key == "EMCURE")
    assert emcure.wins == 2 and emcure.losses == 1
    assert emcure.profit_factor == 4.0
    assert emcure.expectancy == 5.0
    assert emcure.win_rate == pytest.approx(66.7, abs=0.1)


def test_best_stock_by_expectancy(conn):
    # Need ≥5 samples per group to qualify as a leader.
    for _ in range(5):
        sid = _signal(conn, "EMCURE", "vwap_pullback", "TRENDING_BULL", 100)
        _outcome(conn, sid, 110, "WIN")
    for _ in range(5):
        sid = _signal(conn, "IRFC", "vwap_pullback", "TRENDING_BULL", 100)
        _outcome(conn, sid, 98, "LOSS")

    best = analytics.best_stock(conn)
    assert best is not None and best.key == "EMCURE"


def test_report_reports_insufficient_data_early(conn):
    sid = _signal(conn, "EMCURE", "sma7_reversion", "SIDEWAYS", 100)
    _outcome(conn, sid, 105, "WIN")
    report = analytics.format_report(conn)
    assert "Insufficient data" in report
