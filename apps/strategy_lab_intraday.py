"""
Intraday validation of apps/strategy_lab's daily-bar verdict.

The daily lab fills entries at the trigger price the moment the day's LOW reaches
it. Live, the bot polls every 5 minutes and fills at whatever is on screen at that
poll — which on 2026-08-20 meant paying ₹1,914 on the 09:15 opening tick rather
than the ₹1,921 trigger. This replays the SAME decide() against real 5-minute bars
so the fill assumption stops being a free parameter.

Faithful to production:
  - one decision per 5-minute bar close (REFRESH_SECONDS=300)
  - SMA7 / ATR / trend from PRIOR CLOSED daily bars only, mirroring
    intraday.exclude_incomplete_today
  - fill at the bar's CLOSE (what the poll would see), not the trigger price
  - while holding, day_high / day_low are the production shadows
    high_since_entry / low_since_entry: seeded at the entry price and advanced
    only by POLLED prices, never by true bar extremes (managed_cycle.py:523-543).
    Using raw session extremes instead fires the touched-target floor on the
    first poll after entry and manufactures hundreds of phantom round-trips.
  - positions carry overnight, like the live CNC managed cycle

yfinance serves ~60 days of 5-minute bars, so this is a short window — it is a
fill-realism check on the daily lab's ranking, NOT an independent edge test.

Run:  python -m apps.strategy_lab_intraday
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import yfinance as yf

from apps.strategy_lab import LIVE_TARGETS, QTY, Variant, _cfg, _params, _row, _stats
from src.emcure.intraday import classify_7d_trend
from src.emcure.managed_cycle import decide
from src.shared.costs import net_pnl
from src.shared.data import fetch_daily


def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    intra = yf.download("EMCURE.NS", period="60d", interval="5m",
                        auto_adjust=True, progress=False)
    if isinstance(intra.columns, pd.MultiIndex):
        intra.columns = intra.columns.get_level_values(0)
    intra = intra.rename(columns=str.lower).dropna(subset=["close"])
    intra["session"] = intra.index.date

    daily = fetch_daily("EMCURE", days=500)
    h, l, c = daily["high"], daily["low"], daily["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    daily["atr14"] = tr.rolling(14).mean()
    daily["d"] = pd.to_datetime(daily["date"]).dt.date
    return intra, daily


def run(intra: pd.DataFrame, daily: pd.DataFrame, v: Variant) -> dict:
    trades: list[dict] = []
    position: dict | None = None
    running_high = running_low = 0.0

    for session, bars in intra.groupby("session"):
        prior = daily[daily["d"] < session]          # prior CLOSED days only
        if len(prior) < 20:
            continue
        sma7 = float(prior["close"].tail(7).mean())
        atr = float(prior["atr14"].iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            continue
        gap_r, sl_r, targets_r = _params(v, sma7, atr)
        cfg = _cfg(gap_r, sl_r, targets_r, v.regime)
        trend = classify_7d_trend(prior)
        recent = list(prior["close"].tail(3))

        for _ts, bar in bars.iterrows():
            close = float(bar.close)

            if position is None:
                market = {
                    "price": close, "day_high": close, "day_low": close,
                    "gap": close - sma7, "sma7": sma7,
                    "trend_7d": trend, "recent_closes": recent,
                }
                if decide(None, market, cfg).action != "reenter":
                    continue
                # Fill at the polled price, exactly as live does. Seed the
                # since-entry extremes at the entry price, like set_position.
                position = {"entry": close, "qty": QTY,
                            "sl": round(close - sl_r, 2), "date": session}
                running_high = running_low = close
                continue

            running_high = max(running_high, close)
            running_low = min(running_low, close)
            market = {"price": close, "day_high": running_high, "day_low": running_low}
            d = decide(position, market, cfg)
            if d.action in ("sell", "exit_sl"):
                exit_px = float(d.price)
                gross = (exit_px - position["entry"]) * QTY
                net, charges = net_pnl(position["entry"], exit_px, QTY, gross)
                trades.append({
                    "entry_date": position["date"], "exit_date": session,
                    "entry": position["entry"], "exit": exit_px,
                    "gross": gross, "net": net, "charges": charges,
                    "kind": "stop" if d.action == "exit_sl" else "target",
                })
                position = None

    return _stats(trades, v), trades


def main() -> int:
    print("Fetching 60d of 5-minute bars + daily context...", flush=True)
    intra, daily = _load()
    sessions = intra["session"].nunique()
    print(f"{len(intra):,} five-minute bars across {sessions} sessions "
          f"({intra.index.min().date()} → {intra.index.max().date()})\n")

    variants = [
        Variant("LIVE  gap ₹20 / stop ₹30 / trend", "fixed", 20.0, 30.0, LIVE_TARGETS, "trend"),
        Variant("A  gap ₹40 / stop ₹30 / trend", "fixed", 40.0, 30.0, LIVE_TARGETS, "trend"),
        Variant("E  1.0×ATR / 2.0×ATR / trend", "atr", 1.0, 2.0, (0.5, 0.8, 1.2), "trend"),
        Variant("F  1.25×ATR / 2.0×ATR / trend", "atr", 1.25, 2.0, (0.5, 0.8, 1.2), "trend"),
        Variant("G  1.0×ATR / 2.0×ATR / stabil", "atr", 1.0, 2.0, (0.5, 0.8, 1.2), "stabilization"),
    ]

    print("=" * 104)
    print(f"INTRADAY REPLAY — 5-min polls, fills at the polled price, NET of charges (qty {QTY})")
    print("=" * 104)
    results = {}
    for v in variants:
        stats, trades = run(intra, daily, v)
        results[v.name] = trades
        print(_row(stats))

    print("\n" + "=" * 104)
    print("TRADE LOG — live config vs the winner")
    print("=" * 104)
    for name in (variants[0].name, variants[4].name):
        print(f"\n  {name}")
        tl = results[name]
        if not tl:
            print("    (no trades)")
            continue
        for t in tl:
            print(f"    {t['entry_date']} ₹{t['entry']:>8,.2f} → {t['exit_date']} "
                  f"₹{t['exit']:>8,.2f}  {t['kind']:<6} net=₹{t['net']:>8,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
