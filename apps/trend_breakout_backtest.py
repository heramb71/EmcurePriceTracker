"""
Throwaway backtest: does a Donchian-channel trend-following breakout have real
edge, to complement (not replace) EMCURE's dip-buying managed cycle?

Motivated by the Aug 2026 stretch where EMCURE rallied ~13% over three weeks
and the dip-buyer correctly stayed flat the whole time (no ₹20 dip below SMA7
ever occurred) — a pure mean-reversion strategy structurally cannot capture a
straight-line uptrend.

EMCURE alone only has ~2 years of listed history (IPO July 2024), which caps
a single-symbol breakout backtest at a handful of trades — not enough to
trust per this repo's own n>=20 discipline (see reversion_generalization.md /
crypto outcome tracking). So this script tests the SAME breakout rule
cross-sectionally across a basket of liquid, long-history Nifty large-caps,
at two classic Donchian parameter pairs, to see whether the concept
generalizes at all before considering it for EMCURE specifically.

  - Enter LONG when today's close breaks above the prior ENTRY_N-day high.
  - Exit when today's close breaks below the prior EXIT_N-day low (trailing).
  - One position at a time per symbol, same cost model as managed_backtest.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.shared.costs import net_pnl
from src.shared.data import fetch_daily

QTY = 8
PARAM_GRID = [(20, 10), (55, 20)]  # classic Turtle-style short/long pairs
DAYS = 2000  # ask for max available; fetch_daily caps to what yfinance has

# Liquid, long-listed large-caps — excludes recent IPOs (ETERNAL, SWIGGY,
# JIOFIN, LODHA, ...) so each symbol contributes a comparable multi-year
# history instead of a truncated one.
UNIVERSE = [
    "EMCURE", "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
    "AXISBANK", "KOTAKBANK", "LT", "ITC", "HINDUNILVR", "BHARTIARTL",
    "MARUTI", "SUNPHARMA", "TATASTEEL", "TATAMOTORS", "WIPRO", "HCLTECH",
    "ULTRACEMCO", "TITAN", "BAJFINANCE", "ASIANPAINT", "NTPC", "POWERGRID",
]


def run(df: pd.DataFrame, entry_n: int, exit_n: int, qty: int) -> list[dict]:
    trades = []
    position = None
    lookback = max(entry_n, exit_n)

    for i in range(lookback, len(df)):
        row = df.iloc[i]
        c = float(row.close)
        date = pd.to_datetime(row.date).date()

        entry_high = float(df["high"].iloc[i - entry_n:i].max())
        exit_low = float(df["low"].iloc[i - exit_n:i].min())

        if position is None:
            if c > entry_high:
                position = {"entry": round(c, 2), "date": date, "qty": qty}
            continue

        if c < exit_low:
            entry = position["entry"]
            gross = (c - entry) * position["qty"]
            net, charges = net_pnl(entry, c, position["qty"], gross)
            trades.append({
                "entry_date": position["date"], "exit_date": date,
                "entry": entry, "exit": round(c, 2), "qty": position["qty"],
                "gross": round(gross, 2), "charges": charges, "net": net,
                "held_days": (date - position["date"]).days, "closed": True,
            })
            position = None

    if position is not None:
        last = df.iloc[-1]
        c = float(last.close)
        entry = position["entry"]
        gross = (c - entry) * position["qty"]
        net, charges = net_pnl(entry, c, position["qty"], gross)
        trades.append({
            "entry_date": position["date"], "exit_date": pd.to_datetime(last.date).date(),
            "entry": entry, "exit": round(c, 2), "qty": position["qty"],
            "gross": round(gross, 2), "charges": charges, "net": net,
            "held_days": (pd.to_datetime(last.date).date() - position["date"]).days,
            "closed": False,
        })

    return trades


def summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    closed = [t for t in trades if t["closed"]]
    nets = [t["net"] for t in trades]
    closed_nets = [t["net"] for t in closed]
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    return {
        "label": label,
        "n": len(trades),
        "n_closed": len(closed),
        "n_open": len(trades) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": 100 * len(wins) / len(trades),
        "net_total": sum(nets),
        "net_total_closed_only": sum(closed_nets),
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "avg_net": np.mean(nets),
        "avg_held": np.mean([t["held_days"] for t in trades]),
    }


def main():
    print(f"Fetching max available daily history for {len(UNIVERSE)} symbols…\n")
    data: dict[str, pd.DataFrame] = {}
    for sym in UNIVERSE:
        df = fetch_daily(sym, days=DAYS)
        if df is None or df.empty:
            print(f"  {sym}: no data, skipping")
            continue
        df = df.sort_values("date").reset_index(drop=True)
        data[sym] = df
        span = (pd.to_datetime(df.date.iloc[-1]) - pd.to_datetime(df.date.iloc[0])).days
        print(f"  {sym}: {len(df)} bars, {df.date.iloc[0].date()} → {df.date.iloc[-1].date()} (~{span/365:.1f}y)")

    for entry_n, exit_n in PARAM_GRID:
        print("\n" + "=" * 78)
        print(f"DONCHIAN({entry_n}/{exit_n})  qty={QTY}/symbol  n={len(data)} symbols")
        print("=" * 78)

        all_trades = []
        per_symbol = []
        for sym, df in data.items():
            trades = run(df, entry_n, exit_n, QTY)
            for t in trades:
                t["symbol"] = sym
            all_trades.extend(trades)
            s = summarize(trades, sym)
            if s["n"] > 0:
                per_symbol.append(s)

        agg = summarize(all_trades, "AGGREGATE")
        if agg["n"] == 0:
            print("No trades triggered across the whole universe.")
            continue

        print(f"\nAggregate: {agg['n']} trades ({agg['n_closed']} closed, {agg['n_open']} still open)")
        print(f"Win rate:              {agg['win_rate']:.0f}%  ({agg['wins']}W / {agg['losses']}L)")
        print(f"Net P&L (incl. open):  ₹{agg['net_total']:,.0f}")
        print(f"Net P&L (closed only): ₹{agg['net_total_closed_only']:,.0f}")
        print(f"Profit factor:         {agg['profit_factor']:.2f}")
        print(f"Avg net/trade:         ₹{agg['avg_net']:,.0f}")
        print(f"Avg hold:              {agg['avg_held']:.1f} days")
        if agg["n"] < 20:
            print(f"\n⚠️  n={agg['n']} < 20 — below this repo's evidence bar. Not enough signal to trust.")

        print("\nPer-symbol breakdown (n, win-rate, net ₹):")
        for s in sorted(per_symbol, key=lambda x: -x["net_total"]):
            print(f"  {s['label']:<12} n={s['n']:>2}  wr={s['win_rate']:>3.0f}%  "
                  f"net=₹{s['net_total']:>7,.0f}  closed_net=₹{s['net_total_closed_only']:>7,.0f}")


if __name__ == "__main__":
    main()
