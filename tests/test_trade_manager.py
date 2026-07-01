"""Tests for src/trade_manager.py — explicit-level support and the dynamic
delta in target alerts (so custom delivery/swing levels render correctly, not
the hardcoded +₹10/+20/+25)."""
from __future__ import annotations

import pytest

import src.emcure.trade_manager as tm


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point trade state at a temp file so tests never touch the real
    trade_state.json."""
    monkeypatch.setattr(tm, "_STATE_FILE", str(tmp_path / "trade_state.json"))


# ── set_trade ────────────────────────────────────────────────────────────────

def test_set_trade_default_levels_use_rupee_convention():
    s = tm.set_trade(1000.0, 10, risk_rupees=200.0)
    assert s["t1"] == 1010.0 and s["t2"] == 1020.0 and s["t3"] == 1025.0
    assert s["sl"] == 980.0           # 1000 − 200/10


def test_set_trade_explicit_levels_override_formula():
    s = tm.set_trade(1304.0, 8, sl=1265.0, t1=1330.0, t2=1356.0, t3=1382.0)
    assert (s["sl"], s["t1"], s["t2"], s["t3"]) == (1265.0, 1330.0, 1356.0, 1382.0)
    assert s["entry"] == 1304.0 and s["qty"] == 8


# ── check_and_mark + dynamic alert delta ─────────────────────────────────────

def test_target_hit_carries_next_targets_and_real_delta():
    tm.set_trade(1304.0, 8, sl=1265.0, t1=1330.0, t2=1356.0, t3=1382.0)
    # First call calibrates the high-watermark and fires nothing.
    assert tm.check_and_mark(1304.0, 1304.0, 1304.0) == []
    # A new high above T1 fires T1 with the explicit level + next targets.
    hits = tm.check_and_mark(1331.0, 1331.0, 1304.0)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["label"] == "T1" and hit["level"] == 1330.0
    assert hit["t2"] == 1356.0 and hit["t3"] == 1382.0

    msg = tm.format_target_alert("EMCURE", hit, current_price=1331.0)
    assert "+₹26.00/sh" in msg          # 1330 − 1304, NOT the hardcoded +₹10
    assert "1,356.00" in msg            # real next target T2
    assert "+₹10" not in msg            # the old hardcoded delta is gone


def test_stop_loss_hit_reports_loss():
    tm.set_trade(1304.0, 8, sl=1265.0, t1=1330.0, t2=1356.0, t3=1382.0)
    tm.check_and_mark(1304.0, 1304.0, 1304.0)          # calibrate
    hits = tm.check_and_mark(1266.0, 1304.0, 1264.0)   # day_low pierces SL
    assert [h["label"] for h in hits] == ["SL"]
    assert hits[0]["pnl"] == int(round((1265.0 - 1304.0) * 8))   # −312
