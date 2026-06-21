"""
Throwaway backtest: replay the live managed-cycle auto-trader over the last
~6 months of EMCURE daily bars, reusing the REAL decision logic
(src.managed_cycle.decide) and the REAL CNC cost model (src.costs).

Daily-bar approximation (yfinance only serves ~60d of intraday):
  - SMA7 reference = mean of the previous 7 daily closes (no look-ahead).
  - Re-entry: when flat, if the day's LOW dips >= reentry_gap below SMA7 and the
    7-day trend is not Downward, fill at (sma7 - reentry_gap).
  - While holding, drive the real decide() once per day with:
        price     = that day's close
        day_high  = running max high SINCE ENTRY  (preserves the touched-target
                    floor across overnight gaps — the floor's intent)
        day_low   = that day's low
    Stop is checked on the day's low (capital-protection-first, like live).
  - Costs: real Zerodha CNC delivery charges via src.costs.
"""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd

from src.data import fetch_daily
from src.managed_cycle import ManagedConfig, decide
from src.costs import net_pnl
from src.intraday import classify_7d_trend

# Strategy params — match the live managed-cycle (server .env / defaults).
TARGETS = (15.0, 20.0, 30.0)
QTY = 8
REENTRY_GAP = 20.0
# Two stop scenarios: the code default (100) and the value memory says went live (30).
SL_SCENARIOS = {"SL=₹30 (live)": 30.0, "SL=₹100 (code default)": 100.0}

DAYS = 190  # ~6 calendar months


def make_cfg(sl: float) -> ManagedConfig:
    return ManagedConfig(
        enabled=True, live=False, targets=TARGETS, sl_rupees=sl, qty=QTY,
        reentry_gap=REENTRY_GAP, reach_min_prob=50, max_daily_loss=sl * QTY,
        reentry_cooldown_min=0, block_reentry_after_stop=False,
    )


def run(df: pd.DataFrame, cfg: ManagedConfig) -> dict:
    trades = []
    position = None
    running_high = 0.0

    for i in range(7, len(df)):
        row = df.iloc[i]
        o, h, l, c = float(row.open), float(row.high), float(row.low), float(row.close)
        date = pd.to_datetime(row.date).date()

        # SMA7 from the previous 7 closes (no look-ahead).
        window = df.iloc[i - 7:i]
        sma7 = float(window["close"].mean())
        trend = classify_7d_trend(window)

        if position is None:
            trigger = sma7 - cfg.reentry_gap
            if l <= trigger and trend != "Downward":
                entry = min(o, trigger)  # gap-down opens fill at the open
                position = {"entry": round(entry, 2), "qty": cfg.qty,
                            "sl": round(entry - cfg.sl_rupees, 2),
                            "date": date}
                running_high = h
            continue

        # Holding: preserve the touched-target floor across days.
        running_high = max(running_high, h)
        market = {"price": c, "day_high": running_high, "day_low": l,
                  "gap": 0.0, "trend_7d": trend}
        d = decide(position, market, cfg)

        if d.action in ("sell", "exit_sl"):
            exit_price = d.price
            # exit_sl fills at the stop; a target sell fills at the rung/pullback.
            entry = position["entry"]
            gross = (exit_price - entry) * position["qty"]
            net, charges = net_pnl(entry, exit_price, position["qty"], gross)
            held_days = (date - position["date"]).days
            trades.append({
                "entry_date": position["date"], "exit_date": date,
                "entry": entry, "exit": round(exit_price, 2), "qty": position["qty"],
                "kind": d.action, "gross": round(gross, 2), "charges": charges,
                "net": net, "held_days": held_days, "label": d.label,
            })
            position = None
            running_high = 0.0
        # hold/wait → carry to next day

    # Mark-to-market any still-open position at the last close.
    if position is not None:
        last = df.iloc[-1]
        c = float(last.close)
        entry = position["entry"]
        gross = (c - entry) * position["qty"]
        net, charges = net_pnl(entry, c, position["qty"], gross)
        trades.append({
            "entry_date": position["date"], "exit_date": pd.to_datetime(last.date).date(),
            "entry": entry, "exit": round(c, 2), "qty": position["qty"],
            "kind": "open_mtm", "gross": round(gross, 2), "charges": charges,
            "net": net, "held_days": (pd.to_datetime(last.date).date() - position["date"]).days,
            "label": "open",
        })

    return summarize(trades)


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": [], "n": 0}
    nets = [t["net"] for t in trades]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    return {
        "trades": trades,
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": 100 * len(wins) / len(trades),
        "net_total": sum(nets),
        "gross_total": sum(t["gross"] for t in trades),
        "charges_total": sum(t["charges"] for t in trades),
        "avg_net": np.mean(nets),
        "avg_win": np.mean([t["net"] for t in wins]) if wins else 0,
        "avg_loss": np.mean([t["net"] for t in losses]) if losses else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "avg_held": np.mean([t["held_days"] for t in trades]),
        "stops": sum(1 for t in trades if t["kind"] == "exit_sl"),
        "target_sells": sum(1 for t in trades if t["kind"] == "sell"),
    }


def main():
    print("Fetching ~6 months of EMCURE daily data from yfinance…")
    df = fetch_daily("EMCURE", days=DAYS)
    if df is None or df.empty:
        print("ERROR: no data"); sys.exit(1)
    df = df.sort_values("date").reset_index(drop=True)
    start, end = pd.to_datetime(df.date.iloc[0]).date(), pd.to_datetime(df.date.iloc[-1]).date()
    print(f"Data: {len(df)} trading days  {start} → {end}")
    print(f"Price range: ₹{df.low.min():.0f} – ₹{df.high.max():.0f}   "
          f"(last close ₹{df.close.iloc[-1]:.2f})")
    print(f"Capital deployed per trade: ~₹{QTY * df.close.iloc[-1]:,.0f}  (qty={QTY})")

    for name, sl in SL_SCENARIOS.items():
        cfg = make_cfg(sl)
        r = run(df, cfg)
        print("\n" + "=" * 70)
        print(f"SCENARIO: {name}   targets={TARGETS}  reentry_gap=₹{REENTRY_GAP:.0f}")
        print("=" * 70)
        if r["n"] == 0:
            print("No trades triggered."); continue
        print(f"Trades:           {r['n']}   "
              f"(target-sells={r['target_sells']}, stops={r['stops']})")
        print(f"Win rate:         {r['win_rate']:.0f}%  "
              f"({r['wins']}W / {r['losses']}L)")
        print(f"Net P&L:          ₹{r['net_total']:,.0f}   "
              f"(gross ₹{r['gross_total']:,.0f}, costs ₹{r['charges_total']:,.0f})")
        print(f"Profit factor:    {r['profit_factor']:.2f}")
        print(f"Avg net/trade:    ₹{r['avg_net']:,.0f}   "
              f"(avg win ₹{r['avg_win']:,.0f}, avg loss ₹{r['avg_loss']:,.0f})")
        print(f"Avg hold:         {r['avg_held']:.1f} days")
        roi = 100 * r["net_total"] / (QTY * df.close.iloc[-1])
        print(f"Return on ~₹{QTY*df.close.iloc[-1]:,.0f} deployed: {roi:+.1f}% over 6 months")
        print("\n  date_in → date_out    entry    exit   net₹   kind")
        for t in r["trades"]:
            print(f"  {t['entry_date']} → {t['exit_date']}  "
                  f"{t['entry']:>7.1f} {t['exit']:>7.1f}  {t['net']:>6.0f}  {t['kind']}")


if __name__ == "__main__":
    main()
