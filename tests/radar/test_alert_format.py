"""Tests for radar EOD per-stock summary formatting."""
from __future__ import annotations

from datetime import datetime

from src.radar.alert_format import format_eod_stock
from src.radar.regime import SIDEWAYS, TRENDING_BULL
from tests.radar.conftest import make_features

_NOW = datetime(2026, 6, 23, 15, 35)  # Tue


def test_eod_header_and_ohlc_lines():
    snap = make_features(
        stock="SUZLON", open=70.0, day_high=72.5, day_low=69.0,
        price=71.2, prev_close=70.0,
    )
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "SUZLON — end of day" in msg
    assert "Tue, 23 Jun" in msg
    assert "Closed ₹71.20" in msg
    assert "₹69.00–₹72.50" in msg          # day range on one line


def test_eod_change_pct_uses_prev_close():
    snap = make_features(price=110.0, prev_close=100.0)
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "🟢 +10.00%" in msg


def test_eod_negative_change_red():
    snap = make_features(price=90.0, prev_close=100.0)
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "🔴 -10.00%" in msg


def test_eod_setup_forming_when_deep_below_sma7():
    snap = make_features(price=1380.0, sma7=1410.0, gap_to_sma7=-30.0)
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "🔔" in msg and "buy zone" in msg
    assert "₹1,390.26" in msg              # zone scales with price: 1410 * (1 - 0.014)


def test_eod_zone_scales_for_low_priced_stock():
    # ₹70 stock: a fixed ₹20 zone would be absurd (₹50); percent keeps it sane.
    snap = make_features(stock="SUZLON", price=71.0, sma7=73.0, gap_to_sma7=-2.0)
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "₹71.98" in msg  # 73 * (1 - 0.014)


def test_eod_too_far_when_above_average():
    snap = make_features(price=1450.0, sma7=1410.0, gap_to_sma7=40.0)
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "Above its average" in msg
    assert "₹1,390.26" in msg


def test_eod_mood_is_plain_english():
    snap = make_features(rsi=65.0, macd_hist=0.5)
    msg = format_eod_stock(snap, TRENDING_BULL, now=_NOW)
    assert "Looking strong" in msg            # plain words, no RSI/MACD jargon
    assert "RSI" not in msg and "MACD" not in msg


def test_eod_carries_manual_review_footer():
    snap = make_features()
    msg = format_eod_stock(snap, SIDEWAYS, now=_NOW)
    assert "No automatic execution." in msg
