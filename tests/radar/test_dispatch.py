"""Tests for alert selection: cooldown, daily budget, digest overflow."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.radar import signals
from src.radar.dispatch import AlertGate


def _hit(stock, sig="sma7_reversion"):
    return signals.SignalHit(stock, sig, (), (1.0, 1.0), 0.9, 1.2, 1.5)


def _ranked(*hits):
    return [(h, 90 - i, i + 1) for i, h in enumerate(hits)]


def test_individual_within_budget():
    gate = AlertGate(max_per_day=5, cooldown_minutes=90)
    ranked = _ranked(_hit("EMCURE"), _hit("IRFC", "vwap_pullback"))
    indiv, digest = gate.select(ranked, datetime(2026, 6, 20, 10, 0))
    assert len(indiv) == 2 and digest == []


def test_overflow_goes_to_digest():
    gate = AlertGate(max_per_day=1, cooldown_minutes=90)
    ranked = _ranked(_hit("EMCURE"), _hit("IRFC", "vwap_pullback"))
    indiv, digest = gate.select(ranked, datetime(2026, 6, 20, 10, 0))
    assert len(indiv) == 1
    assert len(digest) == 1
    assert indiv[0][0].stock == "EMCURE"  # highest-ranked wins the budget


def test_cooldown_suppresses_repeat():
    gate = AlertGate(max_per_day=10, cooldown_minutes=90)
    now = datetime(2026, 6, 20, 10, 0)
    gate.select(_ranked(_hit("EMCURE")), now)
    indiv, digest = gate.select(_ranked(_hit("EMCURE")), now + timedelta(minutes=30))
    assert indiv == [] and digest == []  # still cooling down


def test_cooldown_expires():
    gate = AlertGate(max_per_day=10, cooldown_minutes=90)
    now = datetime(2026, 6, 20, 10, 0)
    gate.select(_ranked(_hit("EMCURE")), now)
    indiv, _ = gate.select(_ranked(_hit("EMCURE")), now + timedelta(minutes=120))
    assert len(indiv) == 1


def test_budget_resets_next_day():
    gate = AlertGate(max_per_day=1, cooldown_minutes=1)
    gate.select(_ranked(_hit("EMCURE")), datetime(2026, 6, 20, 10, 0))
    indiv, _ = gate.select(_ranked(_hit("EMCURE")), datetime(2026, 6, 21, 10, 0))
    assert len(indiv) == 1  # new day → budget refreshed
