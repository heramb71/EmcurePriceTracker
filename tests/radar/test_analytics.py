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


# ── Outcome-driven muting: negative combos silenced, positive ones validated ──

def _combo(conn, stock, sig, exit_delta, n):
    for _ in range(n):
        sid = _signal(conn, stock, sig, "SIDEWAYS", 100)
        _outcome(conn, sid, 100 + exit_delta, "WIN" if exit_delta > 0 else "LOSS")


def test_muted_and_validated_combos(conn):
    _combo(conn, "IRFC", "sma7_reversion", -5, n=4)     # proven loser
    _combo(conn, "EMCURE", "sma7_reversion", +8, n=4)   # proven winner
    _combo(conn, "SUZLON", "atr_breakout", -5, n=2)     # too few — undecided

    muted = analytics.muted_combos(conn, min_n=3)
    validated = analytics.validated_combos(conn, min_n=3)
    assert ("IRFC", "sma7_reversion") in muted
    assert ("EMCURE", "sma7_reversion") in validated
    assert ("SUZLON", "atr_breakout") not in muted | validated
    assert not muted & validated
