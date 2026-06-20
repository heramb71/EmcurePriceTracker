"""Tests for confidence scoring: orientation, regime bonus, clamping, ranking."""
from __future__ import annotations

from src.radar import scoring, signals
from src.radar.regime import SIDEWAYS, TRENDING_BULL

from .conftest import make_features


def _sma7_hit(f):
    return signals.detect_sma7_reversion(f, TRENDING_BULL)


def test_confidence_in_range():
    f = make_features(price=1400.0, sma7=1440.0, gap_to_sma7=-40.0, atr=20.0)
    conf = scoring.confidence(f, _sma7_hit(f), TRENDING_BULL)
    assert 0 <= conf <= 100


def test_regime_alignment_raises_confidence():
    f = make_features(price=1400.0, sma7=1440.0, gap_to_sma7=-40.0,
                      vwap=1430.0, rsi=32.0, rvol=2.0, atr=20.0)
    hit = _sma7_hit(f)
    bull = scoring.confidence(f, hit, TRENDING_BULL)
    side = scoring.confidence(f, hit, SIDEWAYS)
    assert bull > side  # TRENDING_BULL adds the full regime weight


def test_reversion_rewards_lower_rsi():
    deep = make_features(price=1400.0, sma7=1440.0, gap_to_sma7=-40.0, rsi=28.0, atr=20.0)
    shallow = make_features(price=1400.0, sma7=1440.0, gap_to_sma7=-40.0, rsi=48.0, atr=20.0)
    assert scoring.confidence(deep, _sma7_hit(deep), TRENDING_BULL) > \
        scoring.confidence(shallow, _sma7_hit(shallow), TRENDING_BULL)


def test_rank_orders_high_to_low_with_rank_index():
    f = make_features()
    a = signals.SignalHit("A", signals.SMA7_REVERSION, (), (1, 1), 0.9, 1.2, 1.0)
    b = signals.SignalHit("B", signals.ATR_BREAKOUT, (), (1, 1), 0.9, 1.2, 1.0)
    ranked = scoring.rank([(a, 60), (b, 90)])
    assert [r[0].stock for r in ranked] == ["B", "A"]
    assert [r[2] for r in ranked] == [1, 2]
