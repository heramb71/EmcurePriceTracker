#!/usr/bin/env python3
"""
Crypto reversion lab — does EMCURE-style SMA7 dip-buying survive on BTC/ETH
AFTER Indian VDA taxes?

Runs the percentage-based, multi-day reversion backtest (src/crypto/reversion)
over a parameter grid on daily history, and reports three layers per combo:

  gross      raw strategy return
  net(fees)  after exchange fees (default 0.2%/side)
  post-tax   after fees + 30%+cess on each winner with NO loss set-off
             (Sec 115BBH — the regime that kills most round-tripping)

GATE (per asset, judged on the BEST post-tax combo):
  PASS requires n ≥ 30 trades, post-tax profit factor ≥ 1.3, and positive
  post-tax expectancy. Anything else is FAIL — same discipline as swing_gate,
  which correctly kept the NSE swing bot from deploying.

Research-only: fetches data, prints tables, writes nothing, alerts nobody.

Run:
  python -m apps.crypto_reversion_lab                # full grid, BTC + ETH
  python -m apps.crypto_reversion_lab --days 1500    # shorter history
  python -m apps.crypto_reversion_lab --fee 0.001    # 0.1%/side exchange
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from src.crypto.costs import net_of_fees_pct, post_tax_pct
from src.crypto.data import fetch_crypto_daily
from src.crypto.reversion import CryptoParams, run, stats

_ASSETS = ("BTC-USD", "ETH-USD")

# Parameter grid — percentages of price, spanning shallow dips to crashes.
_GAPS    = (0.03, 0.05, 0.07)
_TARGETS = (0.03, 0.05, 0.08)
_STOPS   = (0.05, 0.08, 0.10)
_MAX_HOLD_DAYS = 10

# Gate thresholds (post-tax layer).
_MIN_TRADES = 30
_MIN_PF = 1.3


def _evaluate(df, fee: float) -> list[dict]:
    rows = []
    for gap, target, sl in itertools.product(_GAPS, _TARGETS, _STOPS):
        p = CryptoParams(gap_pct=gap, target_pct=target, sl_pct=sl,
                         max_hold_days=_MAX_HOLD_DAYS)
        res = run(df, p)
        if res.n == 0:
            continue
        rows.append({
            "params": p,
            "gross": stats(res.pnl_pct, res.hold_days),
            "fees":  stats([net_of_fees_pct(x, fee) for x in res.pnl_pct], res.hold_days),
            "tax":   stats([post_tax_pct(x, fee) for x in res.pnl_pct], res.hold_days),
        })
    # Rank by what actually lands in the account.
    rows.sort(key=lambda r: r["tax"].expectancy_pct, reverse=True)
    return rows


def _fmt_layer(tag: str, s) -> str:
    pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    return (f"{tag}: PF={pf:<5} E={s.expectancy_pct:+.2f}%/trade "
            f"WR={s.win_rate:.0f}% maxDD={s.max_dd_pct:.0f}%")


def _report_asset(symbol: str, days: int, fee: float) -> bool:
    print(f"\n{'=' * 74}\n{symbol} — {days}d history, fee {fee * 100:.2f}%/side")
    df = fetch_crypto_daily(symbol, days=days)
    if df is None or len(df) < 100:
        print("  ❌ could not fetch enough daily history — skipping")
        return False
    print(f"  bars: {len(df)}  ({str(df['date'].iloc[0])[:10]} → {str(df['date'].iloc[-1])[:10]})")

    rows = _evaluate(df, fee)
    if not rows:
        print("  no combo produced a single trade")
        return False

    print(f"\n  Top combos by POST-TAX expectancy (of {len(rows)} with trades):")
    for r in rows[:5]:
        p, g, t = r["params"], r["gross"], r["tax"]
        print(f"\n  gap≥{p.gap_pct * 100:.0f}%  target +{p.target_pct * 100:.0f}%  "
              f"stop −{p.sl_pct * 100:.0f}%  (n={g.n}, hold {g.avg_hold_days:.1f}d)")
        print(f"    {_fmt_layer('gross   ', g)}")
        print(f"    {_fmt_layer('net-fees', r['fees'])}")
        print(f"    {_fmt_layer('POST-TAX', t)}")

    best = rows[0]["tax"]
    passed = (best.n >= _MIN_TRADES and best.profit_factor >= _MIN_PF
              and best.expectancy_pct > 0)
    verdict = "✅ PASS" if passed else "❌ FAIL"
    print(f"\n  GATE ({symbol}): {verdict} — best post-tax combo: "
          f"n={best.n} PF={best.profit_factor} E={best.expectancy_pct:+.2f}%/trade "
          f"(need n≥{_MIN_TRADES}, PF≥{_MIN_PF}, E>0)")
    return passed


def main() -> int:
    ap = argparse.ArgumentParser(description="Crypto SMA7 reversion backtest lab")
    ap.add_argument("--days", type=int, default=2500, help="daily bars of history")
    ap.add_argument("--fee", type=float, default=0.002, help="exchange fee per side (0.002 = 0.2%)")
    args = ap.parse_args()

    print("Crypto reversion lab — SMA7 dip-buying under Indian VDA tax "
          "(30%+cess per winner, NO loss set-off; 1% TDS excluded as advance tax)")

    results = {sym: _report_asset(sym, args.days, args.fee) for sym in _ASSETS}

    print(f"\n{'=' * 74}\nVERDICT: " + "  ·  ".join(
        f"{s}: {'PASS' if ok else 'FAIL'}" for s, ok in results.items()))
    print("Caveats: in-sample grid search (no walk-forward split yet); daily-bar")
    print("fills assume target/stop prices are attainable; validate any PASS with")
    print("the outcome tracker's live forward record before building execution.")
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
