"""Tests for the Zerodha CNC transaction cost model."""
from __future__ import annotations

from src.shared.costs import compute_charges, net_pnl


def test_charges_positive_and_bounded():
    # ~₹20k turnover round trip → charges dominated by STT (0.1% each side)
    charges = compute_charges(entry=1700.0, exit_price=1720.0, qty=6)
    assert 0 < charges < 50
    # STT alone is ~0.1% of ~20.5k ≈ ₹20.5
    assert charges >= 20


def test_charges_scale_with_quantity():
    small = compute_charges(1700.0, 1720.0, 1)
    big   = compute_charges(1700.0, 1720.0, 10)
    assert big > small


def test_charges_zero_on_invalid_input():
    assert compute_charges(0, 1720.0, 6) == 0.0
    assert compute_charges(1700.0, 0, 6) == 0.0
    assert compute_charges(1700.0, 1720.0, 0) == 0.0


def test_net_pnl_subtracts_charges():
    net, charges = net_pnl(1700.0, 1720.0, 6, gross_pnl=120.0)
    assert charges > 0
    assert net == round(120.0 - charges, 2)


def test_round_trip_charges_adds_the_dp_sell_debit():
    from src.shared.costs import DP_CHARGE_PER_SELL, round_trip_charges
    statutory = compute_charges(1700.0, 1720.0, 6)
    assert round_trip_charges(1700.0, 1720.0, 6) == round(statutory + DP_CHARGE_PER_SELL, 2)
    assert round_trip_charges(0, 1720.0, 6) == 0.0


def test_intraday_charges_hand_computed():
    from src.shared.costs import intraday_charges
    # buy ₹10,000 / sell ₹10,100: brokerage 3.00+3.03, txn 0.597, SEBI 0.0201,
    # GST 18% of those, STT 0.025% of sell, stamp 0.003% of buy.
    expected = 6.03 + 0.59697 + 0.0201 + (6.03 + 0.59697 + 0.0201) * 0.18 + 2.525 + 0.3
    assert intraday_charges(100.0, 101.0, 100) == round(expected, 2)


def test_intraday_brokerage_capped_at_20_per_order():
    from src.shared.costs import intraday_charges
    big = intraday_charges(1000.0, 1000.0, 1000)       # ₹10L each side
    # 0.03% of ₹10L = ₹300 → capped to ₹20 per order (₹40 round trip).
    txn, sebi = 2_000_000 * 0.0000297, 2_000_000 * 0.000001
    assert big == round(40 + txn + sebi + (40 + txn + sebi) * 0.18 + 250 + 30, 2)
    assert intraday_charges(0, 100.0, 10) == 0.0


def test_intraday_much_cheaper_than_delivery():
    from src.shared.costs import intraday_charges
    assert intraday_charges(1700.0, 1720.0, 50) < compute_charges(1700.0, 1720.0, 50)
