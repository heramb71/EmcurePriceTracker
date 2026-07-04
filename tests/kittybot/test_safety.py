"""Safety rails: VIX spike, stale picks, loss-streak halt + resume boundary."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.kittybot.safety import (
    evaluate,
    halt_active,
    loss_streak_halt,
    picks_stale,
    resume_date,
    vix_spike,
)


# ── VIX ──────────────────────────────────────────────────────────────────────
def test_vix_spike_over_threshold():
    assert vix_spike(vix_now=13.0, vix_prev_close=11.0, max_pct=15.0) is True  # +18%


def test_vix_no_spike_under_threshold():
    assert vix_spike(12.0, 11.0, 15.0) is False  # +9%


def test_vix_missing_data_does_not_block():
    assert vix_spike(None, 11.0, 15.0) is False
    assert vix_spike(13.0, None, 15.0) is False
    assert vix_spike(13.0, 0.0, 15.0) is False


# ── staleness ────────────────────────────────────────────────────────────────
def test_picks_fresh_within_window():
    now = datetime(2026, 7, 6, 9, 0)
    gen = datetime(2026, 7, 6, 8, 45)
    assert picks_stale(gen, now, max_age_hours=24.0) is False


def test_picks_stale_past_window():
    now = datetime(2026, 7, 6, 9, 0)
    gen = datetime(2026, 7, 5, 8, 0)  # ~25h earlier
    assert picks_stale(gen, now, max_age_hours=24.0) is True


def test_picks_missing_timestamp_is_stale():
    assert picks_stale(None, datetime(2026, 7, 6, 9, 0), 24.0) is True


def test_picks_stale_handles_aware_and_naive():
    from datetime import timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 7, 6, 8, 45, tzinfo=ist)
    naive_now = datetime(2026, 7, 6, 9, 0)
    # Must not raise on mixed tz-awareness.
    assert picks_stale(aware, naive_now, 24.0) is False


# ── loss streak / halt ───────────────────────────────────────────────────────
def test_loss_streak_halt_threshold():
    assert loss_streak_halt(2, max_days=3) is False
    assert loss_streak_halt(3, max_days=3) is True
    assert loss_streak_halt(4, max_days=3) is True


def test_resume_date_is_next_monday():
    # Halt on Monday 2026-07-06 → resume Monday 2026-07-13.
    assert resume_date(date(2026, 7, 6)) == date(2026, 7, 13)
    # Halt on Friday 2026-07-10 → resume Monday 2026-07-13.
    assert resume_date(date(2026, 7, 10)) == date(2026, 7, 13)


def test_halt_active_window():
    resume = date(2026, 7, 13)
    assert halt_active(resume, date(2026, 7, 9)) is True
    assert halt_active(resume, date(2026, 7, 13)) is False  # resumes on the day
    assert halt_active(None, date(2026, 7, 9)) is False


# ── combined decision ────────────────────────────────────────────────────────
def test_evaluate_blocks_on_any_rail():
    now = datetime(2026, 7, 6, 9, 30)
    decision = evaluate(
        vix_now=14.0, vix_prev_close=11.0, vix_spike_pct=15.0,  # +27% → block
        generated_at=datetime(2026, 7, 6, 8, 45), now=now,
        picks_max_age_hours=24.0, halt_until=None,
    )
    assert decision.skip_day is True
    assert any("VIX" in r for r in decision.reasons)


def test_evaluate_clears_when_all_ok():
    now = datetime(2026, 7, 6, 9, 30)
    decision = evaluate(
        vix_now=11.2, vix_prev_close=11.0, vix_spike_pct=15.0,
        generated_at=datetime(2026, 7, 6, 8, 45), now=now,
        picks_max_age_hours=24.0, halt_until=None,
    )
    assert decision.skip_day is False
    assert decision.reasons == []
