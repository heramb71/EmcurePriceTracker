"""Selection + opening-range breakout logic — the day's single-trade choice."""
from __future__ import annotations

from src.kittybot.opening_range import LONG, SHORT, Trigger, breakout_trigger
from src.kittybot.selection import select_trigger

from .conftest import make_or, make_pick


# ── select_trigger ───────────────────────────────────────────────────────────
def test_select_none_when_empty():
    assert select_trigger([]) is None


def test_select_picks_strongest():
    weak = Trigger("A", LONG, 101.0, strength=1.0)
    strong = Trigger("B", LONG, 101.0, strength=5.0)
    assert select_trigger([weak, strong]) is strong


def test_select_tie_breaks_by_symbol_deterministically():
    first = Trigger("AAA", LONG, 101.0, strength=2.0)
    second = Trigger("ZZZ", LONG, 101.0, strength=2.0)
    # Same strength → alphabetical symbol wins, regardless of input order.
    assert select_trigger([second, first]) is first
    assert select_trigger([first, second]) is first


# ── breakout_trigger: LONG ───────────────────────────────────────────────────
def test_long_breakout_above_range_high():
    pick = make_pick(atr14_pct=2.0)
    or_range = make_or(high=105.0, low=100.0, avg_volume=1000.0)
    trig = breakout_trigger(pick, or_range, price=106.0, breakout_volume=1500.0,
                            volume_multiple=1.0)
    assert trig is not None
    assert trig.direction == LONG
    assert trig.trigger_price == 106.0
    assert trig.strength > 0


def test_no_trigger_inside_range():
    pick = make_pick()
    or_range = make_or(high=105.0, low=100.0)
    assert breakout_trigger(pick, or_range, 103.0, 2000.0, 1.0) is None


def test_long_blocked_by_below_average_volume():
    pick = make_pick()
    or_range = make_or(high=105.0, low=100.0, avg_volume=2000.0)
    # Breakout price is above the high, but volume is below the 1× baseline.
    assert breakout_trigger(pick, or_range, 106.0, breakout_volume=1000.0,
                            volume_multiple=1.0) is None


# ── breakout_trigger: SHORT gating on room ───────────────────────────────────
def test_short_allowed_when_short_room_ge_long_room():
    pick = make_pick(long_room_2pct=1.0, short_room_2pct=3.0)
    or_range = make_or(high=105.0, low=100.0, avg_volume=1000.0)
    trig = breakout_trigger(pick, or_range, price=99.0, breakout_volume=1500.0,
                            volume_multiple=1.0)
    assert trig is not None
    assert trig.direction == SHORT


def test_short_skipped_when_long_room_dominates():
    pick = make_pick(long_room_2pct=3.0, short_room_2pct=1.0)
    or_range = make_or(high=105.0, low=100.0, avg_volume=1000.0)
    # Price breaks the low, but the pick has more upside room → skip the short.
    assert breakout_trigger(pick, or_range, 99.0, 1500.0, 1.0) is None


def test_strength_rewards_volume_surge():
    pick = make_pick(atr14_pct=2.0)
    or_range = make_or(high=105.0, low=100.0, avg_volume=1000.0)
    quiet = breakout_trigger(pick, or_range, 106.0, breakout_volume=1000.0, volume_multiple=1.0)
    loud = breakout_trigger(pick, or_range, 106.0, breakout_volume=4000.0, volume_multiple=1.0)
    assert loud.strength > quiet.strength


def test_strength_normalised_by_atr():
    or_range = make_or(high=105.0, low=100.0, avg_volume=1000.0)
    calm = make_pick(atr14_pct=5.0)
    jumpy = make_pick(atr14_pct=1.0)
    # Same absolute poke beyond the high scores higher for the lower-ATR name.
    calm_trig = breakout_trigger(calm, or_range, 106.0, 1000.0, 1.0)
    jumpy_trig = breakout_trigger(jumpy, or_range, 106.0, 1000.0, 1.0)
    assert jumpy_trig.strength > calm_trig.strength
