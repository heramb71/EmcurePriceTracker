"""Entry filters: earnings-today and opening-gap discards."""
from __future__ import annotations

from datetime import date

from src.kittybot.filters import (
    OpenQuote,
    apply_filters,
    discard_reason,
    gap_pct,
    gap_too_large,
    is_earnings_today,
)

from .conftest import make_pick

TODAY = date(2026, 7, 6)


def test_gap_pct_signed():
    assert gap_pct(OpenQuote(open=101.0, prev_close=100.0)) == 1.0
    assert gap_pct(OpenQuote(open=98.0, prev_close=100.0)) == -2.0


def test_gap_pct_zero_when_no_prev_close():
    assert gap_pct(OpenQuote(open=101.0, prev_close=0.0)) == 0.0


def test_gap_too_large_uses_absolute():
    assert gap_too_large(OpenQuote(102.0, 100.0), max_pct=1.5) is True
    assert gap_too_large(OpenQuote(98.0, 100.0), max_pct=1.5) is True
    assert gap_too_large(OpenQuote(101.0, 100.0), max_pct=1.5) is False


def test_earnings_today_via_flag():
    assert is_earnings_today(make_pick(earnings_today=True), TODAY) is True


def test_earnings_today_via_date():
    pick = make_pick(earnings_today=False, earnings_date=TODAY.isoformat())
    assert is_earnings_today(pick, TODAY) is True


def test_earnings_other_day_not_flagged():
    pick = make_pick(earnings_today=False, earnings_date="2026-07-07")
    assert is_earnings_today(pick, TODAY) is False


def test_discard_reason_priority_earnings_first():
    pick = make_pick(earnings_today=True)
    reason = discard_reason(pick, OpenQuote(105.0, 100.0), TODAY, 1.5)
    assert "earnings" in reason


def test_discard_reason_missing_quote():
    assert discard_reason(make_pick(), None, TODAY, 1.5) == "no opening quote available"


def test_discard_reason_none_when_clean():
    assert discard_reason(make_pick(), OpenQuote(100.5, 100.0), TODAY, 1.5) is None


def test_apply_filters_splits_kept_and_discarded():
    picks = (
        make_pick("CLEAN"),
        make_pick("EARN", earnings_today=True),
        make_pick("GAPPY"),
        make_pick("NOQUOTE"),
    )
    quotes = {
        "CLEAN": OpenQuote(100.5, 100.0),
        "EARN": OpenQuote(100.0, 100.0),
        "GAPPY": OpenQuote(103.0, 100.0),  # +3% gap
        # NOQUOTE intentionally absent
    }
    kept, discarded = apply_filters(picks, quotes, TODAY, max_gap_pct=1.5)
    assert [p.symbol for p in kept] == ["CLEAN"]
    discarded_symbols = {p.symbol for p, _ in discarded}
    assert discarded_symbols == {"EARN", "GAPPY", "NOQUOTE"}
