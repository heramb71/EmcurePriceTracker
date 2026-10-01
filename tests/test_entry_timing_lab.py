"""Tests for the EMCURE managed-cycle entry-timing lab."""
from __future__ import annotations

from datetime import time as dtime

import pandas as pd

from src.emcure.entry_timing_lab import add_atr14, replay, session_context
from src.emcure.managed_cycle import ManagedConfig

SESSION = "2026-09-01"


def cfg(**kw) -> ManagedConfig:
    base = dict(enabled=True, live=False, targets=(15.0, 20.0, 30.0), sl_rupees=30.0, qty=8,
                reentry_gap=20.0, reach_min_prob=50, max_daily_loss=0.0,
                reentry_cooldown_min=0.0, block_reentry_after_stop=False, regime_filter="off")
    base.update(kw)
    return ManagedConfig(**base)


def daily(with_today_close: float | None = None) -> pd.DataFrame:
    """30 flat sessions at ₹1000 before SESSION → SMA7 = 1000."""
    dates = pd.bdate_range(end="2026-08-31", periods=30)
    df = pd.DataFrame({"date": dates, "open": 1000.0, "high": 1005.0, "low": 995.0,
                       "close": 1000.0, "volume": 1e5})
    if with_today_close is not None:
        today = {"date": pd.Timestamp(SESSION), "open": 1000.0, "high": 2000.0,
                 "low": 100.0, "close": with_today_close, "volume": 1e5}
        df = pd.concat([df, pd.DataFrame([today])], ignore_index=True)
    return add_atr14(df)


def intra(closes: list[tuple[str, float]], day=SESSION) -> pd.DataFrame:
    return pd.DataFrame({"date": [pd.Timestamp(f"{day} {t}") for t, _ in closes],
                         "close": [c for _, c in closes]})


# 09:15 bar (closes 09:20) is the dip; by 09:25 (closes 09:30) price has recovered.
DIP_AT_OPEN = [("09:15", 975.0), ("09:20", 985.0), ("09:25", 990.0), ("09:30", 991.0),
               ("09:35", 995.0), ("09:40", 1010.0)]


def test_context_uses_only_prior_daily_bars():
    ctx = session_context(daily(with_today_close=5000.0), pd.Timestamp(SESSION).date())
    assert ctx["sma7"] == 1000.0 and ctx["atr14"] == 10.0


def test_live_buys_the_opening_dip_and_books_the_floor():
    trades = replay(intra(DIP_AT_OPEN), daily(), cfg(), None)
    assert len(trades) == 1
    t = trades[0]
    assert t["entry"] == 975.0 and t["kind"] == "target"
    # +₹15 rung (990) touched and price is back at it → floor sold at 09:25.
    assert t["exit"] == 990.0 and t["net"] == round(t["gross"] - t["charges"], 2)


def test_gate_skips_a_dip_that_recovers_before_the_gate():
    assert replay(intra(DIP_AT_OPEN), daily(), cfg(), dtime(9, 30)) == []


def test_gate_still_buys_a_dip_that_persists():
    rows = [("09:15", 990.0), ("09:20", 985.0), ("09:25", 978.0), ("09:30", 1010.0)]
    (t,) = replay(intra(rows), daily(), cfg(), dtime(9, 30))
    assert t["entry"] == 978.0 and t["entry_ts"] == pd.Timestamp(f"{SESSION} 09:25")


def test_stop_fill_is_never_better_than_the_polled_price():
    rows = [("09:15", 975.0), ("09:20", 930.0)]          # sl = 945, gapped through
    (t,) = replay(intra(rows), daily(), cfg(), None)
    assert t["kind"] == "stop" and t["exit"] == 930.0


def test_no_reentry_after_a_stop_when_guard_on():
    rows = [("09:15", 975.0), ("09:20", 940.0), ("09:25", 975.0), ("09:30", 1010.0)]
    assert len(replay(intra(rows), daily(), cfg(block_reentry_after_stop=False), None)) == 2
    assert len(replay(intra(rows), daily(), cfg(block_reentry_after_stop=True), None)) == 1


def test_exits_are_not_gated_by_the_entry_time():
    rows = [("15:20", 975.0)] + [("09:15", 1010.0)]
    frame = pd.concat([intra(rows[:1]), intra(rows[1:], day="2026-09-02")], ignore_index=True)
    (t,) = replay(frame, daily(), cfg(), dtime(9, 45))
    assert t["exit_ts"] == pd.Timestamp("2026-09-02 09:15") and t["kind"] == "target"
