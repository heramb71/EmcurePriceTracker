"""Tests for the liveness heartbeat and the watchdog decision core."""
from __future__ import annotations

from datetime import datetime, timezone

from apps.watchdog import evaluate, is_market_hours
from src.shared import heartbeat

_IST = timezone.utc  # tz-naive comparisons below use explicit datetimes


def test_beat_then_age_is_small(tmp_path):
    path = str(tmp_path / "hb.json")
    heartbeat.beat("emcure-tracker", path=path)
    age = heartbeat.age_seconds(path)
    assert age is not None and age < 5


def test_age_none_when_no_heartbeat(tmp_path):
    assert heartbeat.age_seconds(str(tmp_path / "missing.json")) is None


def test_last_beat_records_component_and_pid(tmp_path):
    path = str(tmp_path / "hb.json")
    heartbeat.beat("emcure-tracker", path=path)
    beat = heartbeat.last_beat(path)
    assert beat["component"] == "emcure-tracker"
    assert isinstance(beat["pid"], int)


def test_age_uses_injected_now(tmp_path):
    path = str(tmp_path / "hb.json")
    heartbeat.beat("x", path=path)
    later = heartbeat.last_beat(path)["ts"] + 1000
    assert heartbeat.age_seconds(path, now=later) >= 1000


# ── watchdog.evaluate — pure decision core ────────────────────────────────────

THRESHOLD = 15 * 60


def test_no_alarm_outside_market_hours_even_if_dead():
    assert evaluate(age=None, in_hours=False, threshold=THRESHOLD) is None
    assert evaluate(age=99999, in_hours=False, threshold=THRESHOLD) is None


def test_alarm_when_no_heartbeat_during_market_hours():
    msg = evaluate(age=None, in_hours=True, threshold=THRESHOLD)
    assert msg is not None and "DOWN" in msg


def test_alarm_when_stale_during_market_hours():
    msg = evaluate(age=THRESHOLD + 60, in_hours=True, threshold=THRESHOLD)
    assert msg is not None and "STALLED" in msg


def test_no_alarm_when_fresh_during_market_hours():
    assert evaluate(age=60, in_hours=True, threshold=THRESHOLD) is None


def test_market_hours_false_on_weekend():
    saturday = datetime(2026, 7, 4, 11, 0, tzinfo=_IST)  # 2026-07-04 is a Saturday
    assert is_market_hours(saturday) is False


def test_market_hours_false_before_open():
    weekday_early = datetime(2026, 7, 3, 8, 0, tzinfo=_IST)  # Friday 08:00
    assert is_market_hours(weekday_early) is False


# ── Multi-component watchdog ──────────────────────────────────────────────────

def test_component_path_is_distinct_per_component():
    from src.shared.heartbeat import component_path
    p = component_path("emcure-bot")
    assert p.endswith("heartbeat-emcure-bot.json")
    assert component_path("emcure-bot") != component_path("emcure-radar")


def test_evaluate_optional_component_skips_missing_heartbeat():
    # The bot's beat only exists when its Telegram poller runs — a missing file
    # must not alarm, but a stale one must.
    assert evaluate(age=None, in_hours=True, threshold=THRESHOLD,
                    component="EMCURE bot", unit="emcure-bot",
                    require_present=False) is None
    msg = evaluate(age=THRESHOLD + 60, in_hours=True, threshold=THRESHOLD,
                   component="EMCURE bot", unit="emcure-bot",
                   require_present=False)
    assert msg is not None and "EMCURE bot" in msg and "emcure-bot" in msg
