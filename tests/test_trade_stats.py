"""Tests for the shared research-lab trade statistics."""
from __future__ import annotations

from src.shared.trade_stats import beats, summarize


def test_empty_is_no_evidence():
    s = summarize([])
    assert s["n"] == 0 and s["pf"] is None and s["expectancy"] == 0.0


def test_basic_numbers():
    s = summarize([100.0, -50.0, 200.0, -100.0])
    assert s["n"] == 4 and s["wins"] == 2 and s["win_pct"] == 50.0
    assert s["pf"] == 2.0                     # 300 / 150
    assert s["expectancy"] == 37.5 and s["net"] == 150.0
    assert s["max_dd"] == 100.0               # peak 250 → 150


def test_no_losers_is_infinite_pf_and_zero_counts_as_loss():
    assert summarize([10.0, 5.0])["pf"] == float("inf")
    assert summarize([10.0, 0.0])["wins"] == 1


def test_beats_needs_sample_size_and_both_metrics():
    live = summarize([10.0, -10.0] * 15)
    better = summarize([20.0, -10.0] * 15)
    assert beats(better, live)
    assert not beats(summarize([20.0, -10.0] * 5), live)        # n < 20
    higher_exp_lower_pf = summarize([100.0, -90.0, -1.0] * 10)  # exp↑ but PF↓
    assert not beats(higher_exp_lower_pf, summarize([1.0, -2.0, 3.0] * 10))
