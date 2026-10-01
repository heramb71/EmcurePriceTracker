"""Tests for the opening-range timing lab (src/kittybot/orb_lab.py)."""
from __future__ import annotations

from datetime import time as dtime

import pandas as pd

from src.kittybot.config import KittyBotConfig
from src.kittybot.opening_range import LONG, SHORT
from src.kittybot.orb_lab import EOD, VARIANTS, Candidate, find_entry, simulate_day, walk_exit
from src.kittybot.risk import STOP, TARGET, TIME, TradePlan
from tests.kittybot.conftest import make_pick

LIVE, OR30, HOLD = VARIANTS
CFG = KittyBotConfig()
DAY = "2026-09-01"


def bars(rows, day=DAY) -> pd.DataFrame:
    """rows: (HH:MM, open, high, low, close[, volume]) → 5-minute bar frame."""
    out = []
    for r in rows:
        hhmm, o, h, lo, c = r[:5]
        out.append({"date": pd.Timestamp(f"{day} {hhmm}"), "open": o, "high": h,
                    "low": lo, "close": c, "volume": r[5] if len(r) > 5 else 1000.0})
    return pd.DataFrame(out)


OPENING = [("09:15", 100, 101, 99, 100), ("09:20", 100, 101, 99, 100),
           ("09:25", 100, 101, 99, 100)]                     # OR15 = 99–101


def cand(rows, symbol="AAA", prev_close=100.0, per_min=0.0, **pick) -> Candidate:
    p = make_pick(symbol, suggested_target_pct=2.0, suggested_stop_pct=1.0,
                  long_room_2pct=1.0, short_room_2pct=1.0, **pick)
    return Candidate(p, prev_close, per_min, bars(rows))


def test_live_enters_on_first_close_after_the_15min_range():
    c = cand(OPENING + [("09:30", 100, 102, 100, 101.5), ("09:35", 101, 101, 100, 100.5)])
    trig, _, ts = find_entry([c], LIVE, CFG, 5)
    assert trig.direction == LONG and trig.trigger_price == 101.5
    assert ts == pd.Timestamp(f"{DAY} 09:30")


def test_30min_range_absorbs_an_early_poke():
    # The 09:30 poke above 101 becomes part of the 30-min range (high 102), so
    # a later close at 101.8 is NOT a breakout under OR30 — only a close > 102 is.
    rows = OPENING + [("09:30", 100, 102, 100, 101.5), ("09:35", 101, 101.5, 100, 101),
                      ("09:40", 101, 101.5, 100, 101), ("09:45", 101, 102, 101, 101.8),
                      ("09:50", 102, 103, 102, 102.5)]
    trig, _, ts = find_entry([cand(rows)], OR30, CFG, 5)
    assert ts == pd.Timestamp(f"{DAY} 09:50") and trig.trigger_price == 102.5


def test_hold_variant_skips_a_fakeout_that_fails_by_0945():
    rows = OPENING + [("09:30", 100, 102, 100, 101.5), ("09:35", 101, 101, 100, 100.5),
                      ("09:40", 100, 100.5, 99.5, 100), ("09:45", 100, 100.5, 99.5, 100)]
    assert find_entry([cand(rows)], LIVE, CFG, 5) is not None
    assert find_entry([cand(rows)], HOLD, CFG, 5) is None


def test_hold_variant_enters_at_0945_when_breakout_holds():
    rows = OPENING + [("09:30", 100, 102, 100, 101.5), ("09:35", 101, 102, 101, 101.6),
                      ("09:40", 101, 102, 101, 101.7)]
    trig, _, ts = find_entry([cand(rows)], HOLD, CFG, 5)
    assert ts == pd.Timestamp(f"{DAY} 09:40") and trig.trigger_price == 101.7  # closes 09:45


def test_no_entry_at_or_after_the_1030_cutoff():
    rows = OPENING + [("10:25", 100, 102, 100, 101.5)]       # closes 10:30
    assert find_entry([cand(rows)], LIVE, CFG, 5) is None


def test_volume_baseline_blocks_quiet_breakouts():
    rows = OPENING + [("09:30", 100, 102, 100, 101.5, 1000.0)]
    assert find_entry([cand(rows, per_min=1000.0)], LIVE, CFG, 5) is None   # 200/min < 1000
    assert find_entry([cand(rows, per_min=100.0)], LIVE, CFG, 5) is not None


def test_strongest_of_simultaneous_breakouts_wins():
    weak = cand(OPENING + [("09:30", 100, 101.5, 100, 101.2)], "WEAK")
    strong = cand(OPENING + [("09:30", 100, 103, 100, 102.8)], "STRONG")
    trig, c, _ = find_entry([weak, strong], LIVE, CFG, 5)
    assert trig.symbol == "STRONG" and c.pick.symbol == "STRONG"


def plan(direction=LONG, entry=100.0, stop=99.0, target=102.0) -> TradePlan:
    return TradePlan("AAA", direction, entry, stop, target, qty=100, risk_rupees=100.0)


def test_walk_exit_target_and_stop_first_when_both_in_one_bar():
    assert walk_exit(bars([("10:00", 100, 102.5, 99.5, 102)]), plan(), CFG, 5) == (TARGET, 102.0)
    assert walk_exit(bars([("10:00", 100, 102.5, 98.5, 100)]), plan(), CFG, 5) == (STOP, 99.0)


def test_walk_exit_short_mirrors():
    p = plan(SHORT, 100.0, 101.0, 98.0)
    assert walk_exit(bars([("10:00", 100, 100.5, 97.5, 98)]), p, CFG, 5) == (TARGET, 98.0)


def test_breakeven_ratchet_then_stop_at_entry():
    rows = [("10:00", 100, 101.2, 100, 101), ("10:05", 101, 101, 99.9, 100)]
    assert walk_exit(bars(rows), plan(), CFG, 5) == (STOP, 100.0)


def test_hard_exit_at_1510_bar_close_and_eod_fallback():
    assert walk_exit(bars([("15:05", 100, 100.5, 99.5, 100.3)]), plan(), CFG, 5) == (TIME, 100.3)
    assert walk_exit(bars([("14:00", 100, 100.5, 99.5, 100.2)]), plan(), CFG, 5) == (EOD, 100.2)


def test_simulate_day_nets_out_charges_and_respects_gap_filter():
    rows = OPENING + [("09:30", 100, 102, 100, 101.5), ("09:35", 101.5, 104, 101.5, 103.5)]
    t = simulate_day([cand(rows)], LIVE, CFG, 5)
    assert t["reason"] == TARGET and t["entry_time"] == dtime(9, 35)
    assert t["gross"] > 0 and 0 < t["charges"] and t["net"] == round(t["gross"] - t["charges"], 2)
    gapped = cand(rows, prev_close=90.0)                      # opens +11% → discarded
    assert simulate_day([gapped], LIVE, CFG, 5) is None
