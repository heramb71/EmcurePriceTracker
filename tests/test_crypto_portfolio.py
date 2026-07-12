"""Tests for the portfolio-aware crypto targets (src/crypto/portfolio.py)."""
from __future__ import annotations

import json

import pytest

from src.crypto import portfolio as pf
from src.crypto.costs import DEFAULT_FEE_PER_SIDE, VDA_TAX_RATE
from src.crypto.messages import format_morning_briefing, format_signal_alert
from src.crypto.portfolio_messages import (
    format_book_profit_alert,
    format_dip_buy_alert,
    format_portfolio_block,
    format_signal_position_note,
)

ETH = {"qty": 0.1816, "invested_inr": 31080.0}
BTC = {"qty": 0.0033497, "invested_inr": 20700.0}


def _write(tmp_path, payload) -> str:
    p = tmp_path / "crypto_portfolio.json"
    p.write_text(json.dumps(payload))
    return str(p)


# ── load_portfolio ───────────────────────────────────────────────────────────

def test_load_missing_file_returns_none(tmp_path):
    assert pf.load_portfolio(str(tmp_path / "nope.json")) is None


def test_load_malformed_json_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert pf.load_portfolio(str(p)) is None


def test_load_valid_merges_plan_defaults(tmp_path):
    path = _write(tmp_path, {"holdings": {"eth": ETH}, "plan": {"budget_inr": 50000}})
    port = pf.load_portfolio(path)
    assert port is not None
    assert "ETH" in port["holdings"]          # symbol upper-cased
    assert port["plan"]["budget_inr"] == 50000
    assert port["plan"]["book_profit_min_pct"] == 20.0   # default merged in


def test_load_skips_malformed_and_zero_holdings(tmp_path):
    path = _write(tmp_path, {"holdings": {
        "ETH": ETH,
        "BAD": {"qty": "x"},
        "ZERO": {"qty": 0, "invested_inr": 100},
    }})
    port = pf.load_portfolio(path)
    assert set(port["holdings"]) == {"ETH"}


def test_load_all_invalid_returns_none(tmp_path):
    path = _write(tmp_path, {"holdings": {"ZERO": {"qty": 0, "invested_inr": 0}}})
    assert pf.load_portfolio(path) is None


# ── P&L math ─────────────────────────────────────────────────────────────────

def test_holding_stats_gross_pnl():
    stats = pf.holding_stats("ETH", ETH, price_inr=200_000.0)
    assert stats["avg_cost_inr"] == pytest.approx(31080 / 0.1816)
    assert stats["value_inr"] == pytest.approx(0.1816 * 200_000)
    assert stats["pnl_inr"] == pytest.approx(0.1816 * 200_000 - 31080)
    assert stats["pnl_pct"] == pytest.approx(stats["pnl_inr"] / 31080 * 100)


def test_holding_stats_net_taxes_winner():
    stats = pf.holding_stats("ETH", ETH, price_inr=250_000.0)
    gross_frac = stats["pnl_pct"] / 100
    expected_net = (gross_frac - 2 * DEFAULT_FEE_PER_SIDE) * (1 - VDA_TAX_RATE)
    assert stats["net_pnl_pct"] == pytest.approx(expected_net * 100)
    assert stats["net_pnl_inr"] < stats["pnl_inr"]


def test_holding_stats_loss_gets_no_tax_relief():
    stats = pf.holding_stats("ETH", ETH, price_inr=100_000.0)
    gross_frac = stats["pnl_pct"] / 100
    # loss: fees still drag, but no 31.2% haircut (nothing to tax)
    assert stats["net_pnl_pct"] == pytest.approx(
        (gross_frac - 2 * DEFAULT_FEE_PER_SIDE) * 100
    )


# ── targets, tranches, dip zones ─────────────────────────────────────────────

def test_sell_targets_band():
    lo, hi = pf.sell_targets_inr(ETH, pf.DEFAULT_PLAN)
    avg = 31080 / 0.1816
    assert lo == pytest.approx(avg * 1.20)
    assert hi == pytest.approx(avg * 1.30)


def test_tranche_from_budget():
    plan = {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6}
    assert pf.tranche_inr(plan) == pytest.approx(50000 / 6)
    assert pf.tranche_inr(pf.DEFAULT_PLAN) == 0.0


def test_dip_zone_prices():
    sig = {"sma7": 3000.0}
    zone = pf.dip_zone_inr(sig, usd_inr=85.0, plan=pf.DEFAULT_PLAN)
    assert zone[0] == pytest.approx(3000 * 0.95 * 85)
    assert zone[1] == pytest.approx(3000 * 0.93 * 85)
    assert pf.dip_zone_inr({"sma7": 0.0}, 85.0, pf.DEFAULT_PLAN) is None


@pytest.mark.parametrize("gap,expected", [
    (-7.5, "strong_dip"),
    (-7.0, "strong_dip"),
    (-5.0, "dip"),
    (-4.9, None),
    (2.0, None),
])
def test_dip_level_thresholds(gap, expected):
    assert pf.dip_level({"sma7_gap_pct": gap}, pf.DEFAULT_PLAN) == expected


def test_dip_level_without_gap_key():
    assert pf.dip_level({}, pf.DEFAULT_PLAN) is None


# ── book-profit decision ─────────────────────────────────────────────────────

def _stats_at(pct: float) -> dict:
    return {"pnl_pct": pct}


def test_book_profit_above_strong_band_fires_regardless():
    sig = {"rsi": 50.0, "signal": "Buy"}
    assert pf.should_book_profit(_stats_at(35.0), sig, pf.DEFAULT_PLAN) == "strong_book"


def test_book_profit_in_band_needs_dip_scope():
    plan = pf.DEFAULT_PLAN
    hold_sig = {"rsi": 50.0, "signal": "Hold"}
    hot_sig = {"rsi": 70.0, "signal": "Hold"}
    sell_sig = {"rsi": 50.0, "signal": "Sell"}
    assert pf.should_book_profit(_stats_at(25.0), hold_sig, plan) is None
    assert pf.should_book_profit(_stats_at(25.0), hot_sig, plan) == "book"
    assert pf.should_book_profit(_stats_at(25.0), sell_sig, plan) == "book"


def test_book_profit_below_band_never_fires():
    hot_sig = {"rsi": 75.0, "signal": "Strong Sell"}
    assert pf.should_book_profit(_stats_at(10.0), hot_sig, pf.DEFAULT_PLAN) is None


# ── portfolio summary (no loss set-off) ──────────────────────────────────────

def test_summary_totals_and_missing():
    port = {"holdings": {"ETH": ETH, "BTC": BTC, "DOGE": {"qty": 10.1, "invested_inr": 114}},
            "plan": dict(pf.DEFAULT_PLAN)}
    prices = {"ETH": 250_000.0, "BTC": 5_000_000.0}   # ETH winner, BTC loser, DOGE no quote
    summary = pf.portfolio_summary(port, prices)
    assert summary["missing"] == ["DOGE"]
    assert summary["invested_inr"] == pytest.approx(31080 + 20700)
    assert summary["value_inr"] == pytest.approx(0.1816 * 250_000 + 0.0033497 * 5_000_000)
    # no set-off: net total = Σ per-coin nets, each computed alone
    per_coin_net = sum(
        pf.holding_stats(s, h, prices[s])["net_pnl_inr"]
        for s, h in [("ETH", ETH), ("BTC", BTC)]
    )
    assert summary["net_pnl_inr"] == pytest.approx(per_coin_net)


def test_summary_no_prices_returns_none():
    port = {"holdings": {"ETH": ETH}, "plan": dict(pf.DEFAULT_PLAN)}
    assert pf.portfolio_summary(port, {}) is None


# ── message formatting ───────────────────────────────────────────────────────

_QUOTE = {"price_inr": 210_000.0, "price_usd": 2470.0, "change_pct": -1.2, "usd_inr": 85.0}
_SIG = {"rsi": 42.0, "trend": "Ranging", "signal": "Hold",
        "sma7": 2600.0, "sma7_gap_pct": -5.0, "score": 0.5,
        "change_7d_pct": -4.0, "macd_hist": -1.0,
        "bb_lower": 2300.0, "bb_upper": 2700.0, "ema200": 2400.0}


def _port() -> dict:
    return {
        "holdings": {"ETH": ETH, "TUSD": {"qty": 9.9, "invested_inr": 872}},
        "plan": {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6},
    }


def test_portfolio_block_contents():
    port = _port()
    summary = pf.portfolio_summary(port, {"ETH": 210_000.0, "TUSD": 88.0})
    block = format_portfolio_block(port, summary, {"ETH": _SIG}, usd_inr=85.0)
    assert "Your Portfolio" in block
    assert "ETH" in block and "TUSD" in block
    assert "Book-profit zone" in block
    assert "TUSD: ₹" not in block.split("Book-profit")[1]   # stablecoin: no targets
    assert "Dip-buy zones" in block
    assert "after fees & 30% tax" in block


def test_briefing_includes_block_only_when_given():
    quote2, sig2 = dict(_QUOTE), dict(_SIG)
    with_block = format_morning_briefing(_QUOTE, _SIG, quote2, sig2,
                                         portfolio_block="── *Your Portfolio* ──\nETH: x")
    without = format_morning_briefing(_QUOTE, _SIG, quote2, sig2)
    assert "Your Portfolio" in with_block
    assert "Your Portfolio" not in without


def test_book_profit_alert_mentions_net_and_partial_booking():
    stats = pf.holding_stats("ETH", ETH, 220_000.0)
    msg = format_book_profit_alert("Ethereum", "ETH", _QUOTE, stats, _SIG,
                                   pf.DEFAULT_PLAN, "book")
    assert "Book-Profit Zone" in msg
    assert "after fees & 30% tax" in msg
    assert "booking part" in msg


def test_dip_buy_alert_mentions_tranche():
    plan = {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6}
    quote = {**_QUOTE, "price_inr": 160_000.0}   # below ETH avg cost (₹171,145)
    stats = pf.holding_stats("ETH", ETH, 160_000.0)
    msg = format_dip_buy_alert("Ethereum", "ETH", quote, _SIG, stats, plan, "strong_dip")
    assert "Strong Dip" in msg
    assert "tranche" in msg
    assert "₹8,333" in msg
    assert "lowers" in msg  # price below avg cost


# ── position-aware signal alerts ─────────────────────────────────────────────
# A bearish signal alert on a coin held below the book band must NOT tell the
# holder to exit — selling there nets ≈ nothing after fees & tax.

_SELL_SIG = {**_SIG, "rsi": 76.0, "signal": "Sell", "sma7_gap_pct": 1.1}
_ETH_QUOTE = {"price_inr": 172_226.0, "price_usd": 1806.0,
              "change_pct": 1.1, "usd_inr": 95.37}


def test_position_note_bearish_below_band_says_dont_add():
    stats = pf.holding_stats("ETH", ETH, _ETH_QUOTE["price_inr"])   # ≈ +0.7% gross
    plan = {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6}
    note = format_signal_position_note("ETH", _ETH_QUOTE, _SELL_SIG, stats, plan)
    assert "Your position" in note
    assert "book zone" in note
    assert "don't add" in note.lower()
    assert "after fees & tax" in note
    assert "exiting" not in note.lower()        # the generic advice must be gone


def test_position_note_bearish_in_band_points_to_booking():
    stats = pf.holding_stats("ETH", ETH, 215_000.0)   # ≈ +25.6% gross, inside band
    note = format_signal_position_note(
        "ETH", {**_ETH_QUOTE, "price_inr": 215_000.0}, _SELL_SIG, stats, pf.DEFAULT_PLAN)
    assert "book" in note.lower()
    assert "part" in note.lower()               # partial booking, not full exit


def test_position_note_bullish_above_dip_zone_urges_patience():
    buy_sig = {**_SIG, "rsi": 33.0, "signal": "Buy", "sma7_gap_pct": -3.0}
    stats = pf.holding_stats("ETH", ETH, _ETH_QUOTE["price_inr"])
    plan = {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6}
    note = format_signal_position_note("ETH", _ETH_QUOTE, buy_sig, stats, plan)
    assert "dip-buy zone" in note.lower()
    assert "chase" in note.lower() or "wait" in note.lower()


def test_position_note_bullish_in_dip_zone_mentions_tranche():
    buy_sig = {**_SIG, "rsi": 33.0, "signal": "Buy", "sma7_gap_pct": -6.0}
    stats = pf.holding_stats("ETH", ETH, 160_000.0)
    plan = {**pf.DEFAULT_PLAN, "budget_inr": 50000, "budget_months": 6}
    note = format_signal_position_note(
        "ETH", {**_ETH_QUOTE, "price_inr": 160_000.0}, buy_sig, stats, plan)
    assert "tranche" in note.lower()


def test_signal_alert_uses_position_note_instead_of_generic_action():
    stats = pf.holding_stats("ETH", ETH, _ETH_QUOTE["price_inr"])
    note = format_signal_position_note(
        "ETH", _ETH_QUOTE, _SELL_SIG, stats, pf.DEFAULT_PLAN)
    msg = format_signal_alert("Ethereum", "ETH", _ETH_QUOTE, _SELL_SIG,
                              position_note=note)
    assert "Your position" in msg
    assert "reducing or exiting" not in msg


def test_signal_alert_without_note_keeps_generic_action():
    msg = format_signal_alert("Ethereum", "ETH", _ETH_QUOTE, _SELL_SIG)
    assert "reducing or exiting" in msg
