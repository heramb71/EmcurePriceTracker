"""Tests for the persistent alert dedupe map (survives mid-day restarts)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.shared.alert_log import AlertLog

_IST = timezone(timedelta(hours=5, minutes=30))
_NOW = datetime(2026, 7, 3, 9, 5, tzinfo=_IST)


def _log(tmp_path, now=_NOW) -> AlertLog:
    return AlertLog(now=now, path=str(tmp_path / "alerts_sent.json"))


def test_survives_a_same_day_restart(tmp_path):
    log = _log(tmp_path)
    log[f"pre_open_{_NOW.date()}"] = _NOW
    # "Restart": a fresh instance over the same file still knows it was sent.
    log2 = _log(tmp_path)
    assert f"pre_open_{_NOW.date()}" in log2
    assert log2[f"pre_open_{_NOW.date()}"] == _NOW   # aware datetime round-trips


def test_prunes_previous_days_on_load(tmp_path):
    log = _log(tmp_path)
    log[f"pre_open_{_NOW.date()}"] = _NOW
    log2 = _log(tmp_path, now=_NOW + timedelta(days=1))
    assert len(log2) == 0


def test_cooldown_arithmetic_works_after_reload(tmp_path):
    log = _log(tmp_path)
    log["intra_BUY_2026-07-03"] = _NOW
    log2 = _log(tmp_path)
    # The 15-min intraday cooldown must be computable on the reloaded value.
    age = (_NOW + timedelta(minutes=10) - log2["intra_BUY_2026-07-03"]).total_seconds()
    assert age == 600


def test_tolerates_missing_and_corrupt_files(tmp_path):
    assert len(_log(tmp_path)) == 0                       # missing file
    (tmp_path / "alerts_sent.json").write_text("{broken")
    assert len(_log(tmp_path)) == 0                       # corrupt file

    bad = tmp_path / "alerts_sent.json"
    bad.write_text('{"key": 42, "ok": "' + _NOW.isoformat() + '"}')
    log = _log(tmp_path)
    assert list(log) == ["ok"]                            # bad values skipped


# ── max_age retention (crypto: date-less cooldown keys span midnight) ────────

def _aged_log(tmp_path, now) -> AlertLog:
    return AlertLog(now=now, path=str(tmp_path / "alerts.json"),
                    max_age=timedelta(hours=24))


def test_max_age_keeps_cooldown_entry_across_midnight(tmp_path):
    late = datetime(2026, 7, 3, 23, 30, tzinfo=_IST)
    log = _aged_log(tmp_path, late)
    log["signal_ETH"] = late          # 4h cooldown started 23:30
    log2 = _aged_log(tmp_path, late + timedelta(hours=1))   # restart at 00:30
    assert "signal_ETH" in log2       # day-based pruning would have dropped it


def test_max_age_prunes_entries_older_than_window(tmp_path):
    late = datetime(2026, 7, 3, 23, 30, tzinfo=_IST)
    log = _aged_log(tmp_path, late)
    log["signal_ETH"] = late
    log2 = _aged_log(tmp_path, late + timedelta(hours=25))
    assert len(log2) == 0
