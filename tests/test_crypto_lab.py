"""Tests for the crypto reversion lab: VDA cost model, multi-day backtest
engine, and the forward-outcome tracker."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from src.crypto import outcomes
from src.crypto.costs import VDA_TAX_RATE, net_of_fees_pct, post_tax_pct
from src.crypto.reversion import CryptoParams, run, stats

# ── India VDA cost model ──────────────────────────────────────────────────────

def test_fees_drag_both_sides():
    assert net_of_fees_pct(0.05, fee_per_side=0.002) == 0.05 - 0.004


def test_post_tax_taxes_winners_only_no_loss_relief():
    # Winner: fee-adjusted gain taxed at 31.2%.
    win = post_tax_pct(0.05, fee_per_side=0.002)
    assert win == (0.05 - 0.004) * (1 - VDA_TAX_RATE)
    # Loser: full loss plus fees — no set-off, no relief.
    loss = post_tax_pct(-0.05, fee_per_side=0.002)
    assert loss == -0.05 - 0.004


def test_tax_asymmetry_kills_symmetric_coinflips():
    # +5%/−5% at 50/50 is a fair game pre-cost; after fees + VDA tax it must
    # be clearly negative — the whole reason the lab gates post-tax.
    ev = (post_tax_pct(0.05) + post_tax_pct(-0.05)) / 2
    assert ev < -0.005


# ── Reversion engine on synthetic bars ────────────────────────────────────────

def _frame(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df.insert(0, "date", pd.date_range("2026-01-01", periods=len(df)))
    df["volume"] = 1000
    return df


def _flat_week(price: float = 100.0) -> list[tuple[float, float, float, float]]:
    # 7 gently rising bars (not "Downward"), SMA7 ≈ price.
    return [(price + i * 0.2,) * 4 for i in range(7)]


def test_no_trigger_when_open_is_near_sma7():
    df = _frame(_flat_week() + [(100.0, 101.0, 99.0, 100.5)])
    res = run(df, CryptoParams(gap_pct=0.05, target_pct=0.05, sl_pct=0.08))
    assert res.n == 0


def test_dip_entry_then_target_hit_days_later():
    rows = _flat_week()
    rows.append((94.0, 94.5, 93.5, 94.0))    # open ~6% below SMA7 → entry at 94
    rows.append((94.0, 96.0, 93.8, 95.5))    # drifts up
    rows.append((95.5, 99.5, 95.0, 99.0))    # high ≥ 98.7 (94 × 1.05) → target
    df = _frame(rows)
    res = run(df, CryptoParams(gap_pct=0.05, target_pct=0.05, sl_pct=0.08))
    assert res.n == 1
    assert res.outcomes == ["target"]
    assert abs(res.pnl_pct[0] - 0.05) < 1e-9
    assert res.hold_days == [2]


def test_stop_checked_pessimistically_before_target():
    rows = _flat_week()
    rows.append((94.0, 94.5, 93.5, 94.0))    # entry at 94; sl = 86.48 (−8%)
    rows.append((90.0, 99.5, 86.0, 98.0))    # both stop and target print → stop
    df = _frame(rows)
    res = run(df, CryptoParams(gap_pct=0.05, target_pct=0.05, sl_pct=0.08))
    assert res.outcomes == ["stop"]
    assert abs(res.pnl_pct[0] - (-0.08)) < 1e-9


def test_time_exit_at_max_hold_close():
    rows = _flat_week()
    rows.append((94.0, 94.5, 93.5, 94.0))    # entry at 94
    rows += [(94.0, 95.0, 93.0, 94.5)] * 3   # never hits ±
    df = _frame(rows)
    res = run(df, CryptoParams(gap_pct=0.05, target_pct=0.10, sl_pct=0.10,
                               max_hold_days=3))
    assert res.outcomes == ["time_exit"]
    assert res.hold_days == [3]


def test_stats_layers():
    s = stats([0.05, -0.05, 0.05], hold_days=[1, 2, 3])
    assert s.n == 3 and s.win_rate == 66.7
    assert s.profit_factor == 2.0
    assert s.avg_hold_days == 2.0


# ── Forward-outcome tracker ───────────────────────────────────────────────────

_T0 = datetime(2026, 7, 1, 12, 0)


def _conn(tmp_path):
    return outcomes.connect(str(tmp_path / "crypto.db"))


def _oversold_sig(rsi=30.0, signal="Buy", score=0.6):
    return {"rsi": rsi, "signal": signal, "score": score}


def test_classify_alert_directions():
    assert outcomes.classify_alert({"signal": "Strong Buy", "rsi": 50}) == ("strong_buy", 1)
    assert outcomes.classify_alert({"signal": "Strong Sell", "rsi": 50}) == ("strong_sell", -1)
    assert outcomes.classify_alert({"signal": "Hold", "rsi": 30}) == ("oversold", 1)
    assert outcomes.classify_alert({"signal": "Hold", "rsi": 70}) == ("overbought", -1)
    assert outcomes.classify_alert({"signal": "Hold", "rsi": 50}) is None


def test_record_then_outcomes_mature_per_horizon(tmp_path):
    conn = _conn(tmp_path)
    sid = outcomes.record_alert(conn, "BTC", _oversold_sig(), {"price_usd": 100000.0}, _T0)
    assert sid is not None

    # Before 1d matures: nothing books.
    assert outcomes.evaluate_due(conn, {"BTC": 101000.0}, _T0 + timedelta(hours=12)) == 0
    # After 1d: +2% signed ≥ 1.5% → WIN at 1d only.
    assert outcomes.evaluate_due(conn, {"BTC": 102000.0}, _T0 + timedelta(days=1, minutes=5)) == 1
    # After 7d at −6%: 3d and 7d book as LOSS; 1d already booked (no rewrite).
    assert outcomes.evaluate_due(conn, {"BTC": 94000.0}, _T0 + timedelta(days=7, minutes=5)) == 2

    rows = {r["horizon"]: r for r in conn.execute("SELECT * FROM outcomes").fetchall()}
    assert rows["1d"]["outcome"] == "WIN"
    assert rows["3d"]["outcome"] == "LOSS" and rows["7d"]["outcome"] == "LOSS"
    # Re-running writes nothing new (idempotent).
    assert outcomes.evaluate_due(conn, {"BTC": 94000.0}, _T0 + timedelta(days=8)) == 0


def test_signed_pct_flips_for_short_direction(tmp_path):
    conn = _conn(tmp_path)
    outcomes.record_alert(conn, "ETH", _oversold_sig(rsi=75.0), {"price_usd": 4000.0}, _T0)
    outcomes.evaluate_due(conn, {"ETH": 3800.0}, _T0 + timedelta(days=1, minutes=5))
    row = conn.execute("SELECT * FROM outcomes").fetchone()
    assert row["signed_pct"] == 5.0        # price fell 5%, short expected it → +5
    assert row["outcome"] == "WIN"


def test_summary_and_report(tmp_path):
    conn = _conn(tmp_path)
    for delta in (+3.0, +2.0, -4.0):
        sid = outcomes.record_alert(conn, "BTC", _oversold_sig(), {"price_usd": 100.0}, _T0)
        conn.execute(
            "INSERT INTO outcomes (signal_id, horizon, price_usd, signed_pct, outcome, evaluated_at) "
            "VALUES (?, '3d', ?, ?, ?, ?)",
            (sid, 100 + delta, delta, "WIN" if delta >= 3 else ("LOSS" if delta <= -3 else "NEUTRAL"),
             _T0.isoformat()),
        )
    conn.commit()
    blocks = outcomes.summary(conn, "3d")
    assert blocks[0]["symbol"] == "BTC" and blocks[0]["n"] == 3
    assert blocks[0]["wins"] == 1 and blocks[0]["losses"] == 1
    report = outcomes.format_report(conn)
    assert "BTC" in report and "oversold" in report
