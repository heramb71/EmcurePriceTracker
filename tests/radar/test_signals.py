"""Tests for the 5 signal detectors: fire conditions + stop/target geometry."""
from __future__ import annotations

from src.radar import signals
from src.radar.regime import TRENDING_BEAR, TRENDING_BULL

from .conftest import make_features


def test_sma7_reversion_fires_when_stretched_below():
    # 2.5% below SMA7 → fires; target is the SMA7 mean, stop below entry.
    f = make_features(price=1400.0, sma7=1436.0, gap_to_sma7=-36.0, atr=20.0)
    hit = signals.detect_sma7_reversion(f, TRENDING_BULL)
    assert hit is not None
    assert hit.signal_type == signals.SMA7_REVERSION
    assert hit.target == 1436.0
    assert hit.stop < f.price < hit.target
    assert hit.rr > 0


def test_sma7_reversion_stop_respects_rr_floor():
    # Wide ATR would give RR≈0.3 with the old stop; the RR floor keeps RR≥1.
    f = make_features(price=1400.0, sma7=1427.0, gap_to_sma7=-27.0, atr=60.0)
    hit = signals.detect_sma7_reversion(f, TRENDING_BULL)
    assert hit is not None
    assert hit.rr >= 1.0


def test_sma7_reversion_silent_when_near_mean():
    f = make_features(price=1400.0, sma7=1405.0, gap_to_sma7=-5.0)
    assert signals.detect_sma7_reversion(f, TRENDING_BULL) is None


def test_vwap_pullback_fires_on_dip_in_uptrend():
    f = make_features(price=1406.0, ema20=1405.0, ema50=1390.0, rsi=48.0, atr=15.0)
    hit = signals.detect_vwap_pullback(f, TRENDING_BULL)
    assert hit is not None
    assert hit.signal_type == signals.VWAP_PULLBACK


def test_vwap_pullback_silent_in_downtrend():
    f = make_features(ema20=1380.0, ema50=1405.0)  # EMA20 < EMA50
    assert signals.detect_vwap_pullback(f, TRENDING_BULL) is None


def test_rvol_reversal_needs_volume_and_oversold():
    f = make_features(rvol=2.0, rsi=30.0, atr=20.0)
    assert signals.detect_rvol_reversal(f, TRENDING_BULL) is not None
    # RSI not stretched → silent
    assert signals.detect_rvol_reversal(make_features(rvol=2.0, rsi=45.0), TRENDING_BULL) is None
    # Low volume → silent
    assert signals.detect_rvol_reversal(make_features(rvol=1.1, rsi=30.0), TRENDING_BULL) is None


def test_atr_breakout_needs_expansion_above_high_and_vwap():
    f = make_features(price=1420.0, prev_high=1408.0, vwap=1402.0,
                      atr_expansion=1.5, atr=20.0)
    hit = signals.detect_atr_breakout(f, TRENDING_BULL)
    assert hit is not None
    assert hit.signal_type == signals.ATR_BREAKOUT
    # No expansion → silent
    assert signals.detect_atr_breakout(make_features(atr_expansion=1.0), TRENDING_BULL) is None


def test_gap_reversion_fires_on_reclaimed_gap_down():
    f = make_features(prev_close=1450.0, open=1400.0, price=1415.0,
                      gap_pct=-3.4, atr=20.0)
    hit = signals.detect_gap_reversion(f, TRENDING_BULL)
    assert hit is not None
    assert hit.target == 1450.0  # fills to prev close
    assert hit.stop < f.price < hit.target


def test_detect_skips_all_in_bear_regime():
    f = make_features(price=1400.0, sma7=1440.0, gap_to_sma7=-40.0, atr=20.0)
    assert signals.detect(f, TRENDING_BEAR) == []
    assert len(signals.detect(f, TRENDING_BULL)) >= 1
