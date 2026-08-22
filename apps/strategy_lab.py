"""
Strategy lab: does the managed cycle's FIXED-RUPEE calibration still fit EMCURE?

The live entry gap (₹20), stop (₹30) and target ladder (15/20/30) were calibrated
in June 2026 against a ~₹1,733 price and a ~₹19 median daily wiggle. Price is now
~₹1,880 and ATR14 ~₹56, so every one of those constants has silently shrunk in
volatility terms — a ₹30 stop that was ~1.6x the noise floor is now ~0.5x it.

This lab replays the REAL decision logic (src.emcure.managed_cycle.decide) and the
REAL Zerodha cost model (src.shared.costs) over daily bars, comparing three ways of
expressing the same strategy:

    fixed  — today's live config (₹ constants)
    pct    — constants re-expressed as % of SMA7 (scale-invariant vs price)
    atr    — constants re-expressed as multiples of ATR14 (scale-invariant vs
             price AND volatility regime)

Daily-bar approximation (yfinance serves only ~60d of intraday), matching the
conventions already used by apps/managed_backtest.py:
  - SMA7 / ATR / trend are computed from PRIOR CLOSED days only (no look-ahead),
    mirroring intraday.exclude_incomplete_today in production.
  - Entry fills at the trigger price when the day's LOW reaches it.
  - While holding, decide() is driven once per day with day_high = running max
    high SINCE ENTRY (preserves the touched-target floor across overnight gaps).
  - The stop is checked on the day's low, capital-protection-first, like live.

Run:  python -m apps.strategy_lab
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.emcure.intraday import classify_7d_trend
from src.emcure.managed_cycle import ManagedConfig, decide
from src.shared.costs import net_pnl
from src.shared.data import fetch_daily

QTY = 8
DAYS = 500  # ~2y — EMCURE listed Jul 2024, so this is close to its full history.

# Live config, and the ATR/pct multiples that reproduce it at the June-2026
# calibration point (price ~₹1,733, ATR14 ~₹25) — the question this lab asks is
# whether holding the MULTIPLE constant beats holding the RUPEE constant.
LIVE_GAP, LIVE_SL = 20.0, 30.0
LIVE_TARGETS = (15.0, 20.0, 30.0)


@dataclass(frozen=True)
class Variant:
    name: str
    mode: str            # fixed | pct | atr
    gap: float           # ₹ | % of sma7 | × ATR
    sl: float
    targets: tuple[float, ...]
    regime: str = "trend"


def _params(v: Variant, sma7: float, atr: float) -> tuple[float, float, tuple[float, ...]]:
    """Resolve a variant's abstract constants into rupees for this bar."""
    if v.mode == "fixed":
        return v.gap, v.sl, v.targets
    unit = sma7 / 100.0 if v.mode == "pct" else atr
    return v.gap * unit, v.sl * unit, tuple(t * unit for t in v.targets)


def _cfg(gap: float, sl: float, targets: tuple[float, ...], regime: str) -> ManagedConfig:
    return ManagedConfig(
        enabled=True, live=False, targets=targets, sl_rupees=sl, qty=QTY,
        reentry_gap=gap, reach_min_prob=50, max_daily_loss=sl * QTY,
        reentry_cooldown_min=0, block_reentry_after_stop=False,
        regime_filter=regime,
    )


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    out = df.copy()
    out["atr14"] = tr.rolling(14).mean()
    return out


def run(df: pd.DataFrame, v: Variant) -> dict:
    """Replay one variant. Returns trade stats net of real Zerodha charges."""
    trades: list[dict] = []
    position: dict | None = None
    running_high = 0.0

    for i in range(20, len(df)):
        row = df.iloc[i]
        high, low, close = float(row.high), float(row.low), float(row.close)
        prior = df.iloc[:i]                      # prior CLOSED days only
        sma7 = float(prior["close"].tail(7).mean())
        atr = float(prior["atr14"].iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            continue
        gap_r, sl_r, targets_r = _params(v, sma7, atr)
        cfg = _cfg(gap_r, sl_r, targets_r, v.regime)

        if position is None:
            trigger = sma7 - gap_r
            if low > trigger:
                continue                          # never dipped into the zone
            market = {
                "price": trigger, "day_high": high, "day_low": low,
                "gap": -gap_r, "sma7": sma7,
                "trend_7d": classify_7d_trend(prior),
                "recent_closes": list(prior["close"].tail(3)),
            }
            if decide(None, market, cfg).action != "reenter":
                continue
            position = {
                "entry": trigger, "qty": QTY,
                "sl": round(trigger - sl_r, 2),
                "date": pd.to_datetime(row.date).date(),
            }
            running_high = high
            continue

        # Holding — drive the real decide() with the floor-preserving running high.
        running_high = max(running_high, high)
        market = {"price": close, "day_high": running_high, "day_low": low}
        d = decide(position, market, cfg)
        if d.action in ("sell", "exit_sl"):
            exit_px = float(d.price)
            gross = (exit_px - position["entry"]) * QTY
            net, charges = net_pnl(position["entry"], exit_px, QTY, gross)
            trades.append({
                "entry_date": position["date"], "exit_date": pd.to_datetime(row.date).date(),
                "entry": position["entry"], "exit": exit_px,
                "gross": gross, "net": net, "charges": charges,
                "kind": "stop" if d.action == "exit_sl" else "target",
            })
            position = None

    return _stats(trades, v)


def _stats(trades: list[dict], v: Variant) -> dict:
    if not trades:
        return {"variant": v.name, "n": 0}
    nets = np.array([t["net"] for t in trades])
    wins, losses = nets[nets > 0], nets[nets <= 0]
    gross_win, gross_loss = wins.sum(), abs(losses.sum())
    equity = np.cumsum(nets)
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))
    dd = (peak[1:] - equity).max()
    return {
        "variant": v.name, "n": len(trades),
        "win_pct": 100.0 * len(wins) / len(trades),
        "net": nets.sum(),
        "exp": nets.mean(),
        "pf": (gross_win / gross_loss) if gross_loss else float("inf"),
        "maxdd": dd,
        "stops": sum(1 for t in trades if t["kind"] == "stop"),
        "charges": sum(t["charges"] for t in trades),
    }


def _row(s: dict) -> str:
    if not s["n"]:
        return f"  {s['variant']:<34} no trades"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"  {s['variant']:<34} n={s['n']:>3}  win={s['win_pct']:>5.1f}%  "
            f"PF={pf:>5}  exp=₹{s['exp']:>7.0f}  net=₹{s['net']:>8,.0f}  "
            f"maxDD=₹{s['maxdd']:>6,.0f}  stops={s['stops']:>2}")


def main() -> int:
    print(f"Fetching EMCURE daily bars ({DAYS} sessions)...", flush=True)
    df = fetch_daily("EMCURE", days=DAYS)
    if df is None or df.empty:
        print("No data.")
        return 1
    df = _prepare(df).reset_index(drop=True)
    px, atr = float(df["close"].iloc[-1]), float(df["atr14"].iloc[-1])
    print(f"{len(df)} sessions · last close ₹{px:,.2f} · ATR14 ₹{atr:,.2f}\n")

    print("=" * 104)
    print("CALIBRATION DRIFT — what the live ₹ constants are worth today")
    print("=" * 104)
    print(f"  entry gap ₹{LIVE_GAP:.0f} = {LIVE_GAP/atr:.2f}× ATR = {100*LIVE_GAP/px:.2f}% of price")
    print(f"  stop      ₹{LIVE_SL:.0f} = {LIVE_SL/atr:.2f}× ATR = {100*LIVE_SL/px:.2f}% of price")
    print(f"  targets   {LIVE_TARGETS} = "
          f"{tuple(round(t/atr, 2) for t in LIVE_TARGETS)}× ATR")

    variants = [
        Variant("fixed ₹20/₹30/15-20-30 (LIVE)", "fixed", LIVE_GAP, LIVE_SL, LIVE_TARGETS),
        Variant("fixed, no regime gate", "fixed", LIVE_GAP, LIVE_SL, LIVE_TARGETS, "off"),
        Variant("fixed, stabilization gate", "fixed", LIVE_GAP, LIVE_SL, LIVE_TARGETS, "stabilization"),
        Variant("fixed ₹20 gap / ₹50 stop", "fixed", LIVE_GAP, 50.0, LIVE_TARGETS),
        Variant("pct 1.15% gap / 1.7% stop", "pct", 1.15, 1.7, (0.85, 1.15, 1.7)),
        Variant("atr 0.35× gap / 1.0× stop", "atr", 0.35, 1.0, (0.3, 0.5, 0.8)),
        Variant("atr 0.5× gap / 1.0× stop", "atr", 0.5, 1.0, (0.3, 0.5, 0.8)),
        Variant("atr 0.5× gap / 1.5× stop", "atr", 0.5, 1.5, (0.4, 0.7, 1.0)),
        Variant("atr 0.75× gap / 1.5× stop", "atr", 0.75, 1.5, (0.4, 0.7, 1.0)),
        Variant("atr 0.75× gap / 2.0× stop", "atr", 0.75, 2.0, (0.5, 0.8, 1.2)),
        Variant("atr 1.0× gap / 2.0× stop", "atr", 1.0, 2.0, (0.5, 0.8, 1.2)),
    ]

    print("\n" + "=" * 104)
    print(f"VARIANT COMPARISON — {len(df)} sessions, qty {QTY}, NET of real Zerodha CNC charges")
    print("=" * 104)
    for v in variants:
        print(_row(run(df, v)))

    # The variant table trends monotonically better as the gap deepens, which is
    # exactly what an overfit-by-selection would look like. Two checks: sweep past
    # the winner to find where it breaks, then split the window and re-judge.
    print("\n" + "=" * 104)
    print("SWEEP — does 'deeper gap' improve forever? (ATR stop 2.0×, targets 0.5/0.8/1.2×)")
    print("=" * 104)
    for g in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
        print(_row(run(df, Variant(f"atr {g}× gap / 2.0× stop", "atr", g, 2.0, (0.5, 0.8, 1.2)))))

    print("\n" + "=" * 104)
    print("WALK-FORWARD — first half (in-sample) vs second half (out-of-sample)")
    print("=" * 104)
    mid = len(df) // 2
    halves = {
        f"IS  ({df['date'].iloc[20].date()} → {df['date'].iloc[mid-1].date()})": df.iloc[:mid],
        f"OOS ({df['date'].iloc[mid].date()} → {df['date'].iloc[-1].date()})":
            df.iloc[mid - 20:].reset_index(drop=True),
    }
    finalists = [
        Variant("fixed ₹20/₹30 (LIVE)", "fixed", LIVE_GAP, LIVE_SL, LIVE_TARGETS),
        Variant("atr 0.75× / 2.0×", "atr", 0.75, 2.0, (0.5, 0.8, 1.2)),
        Variant("atr 1.0× / 2.0×", "atr", 1.0, 2.0, (0.5, 0.8, 1.2)),
        Variant("atr 1.25× / 2.0×", "atr", 1.25, 2.0, (0.5, 0.8, 1.2)),
    ]
    for label, part in halves.items():
        print(f"\n  {label}")
        for v in finalists:
            print(_row(run(part, v)))

    # gap, stop and targets all moved together above, so the win could belong to
    # any one of them. Change one axis at a time, off the live config.
    print("\n" + "=" * 104)
    print("ABLATION — one axis at a time off the LIVE config (full window)")
    print("=" * 104)
    print("  [a] stop only — keep live ₹20 gap and 15/20/30 targets")
    for sl in (30.0, 50.0, 75.0, 100.0, 125.0):
        print(_row(run(df, Variant(f"    gap ₹20, stop ₹{sl:.0f}", "fixed", 20.0, sl, LIVE_TARGETS))))
    print("\n  [b] gap only — keep live ₹30 stop and 15/20/30 targets")
    for g in (20.0, 30.0, 40.0, 50.0, 60.0):
        print(_row(run(df, Variant(f"    gap ₹{g:.0f}, stop ₹30", "fixed", g, 30.0, LIVE_TARGETS))))
    print("\n  [c] gap + stop together, targets still live 15/20/30")
    for g, sl in ((30.0, 60.0), (40.0, 80.0), (50.0, 100.0), (60.0, 120.0)):
        print(_row(run(df, Variant(f"    gap ₹{g:.0f}, stop ₹{sl:.0f}", "fixed", g, sl, LIVE_TARGETS))))

    print("\n" + "=" * 104)
    print("REGIME GATE — does the winner still want the trend filter?")
    print("=" * 104)
    for regime in ("trend", "off", "stabilization"):
        print(_row(run(df, Variant(f"atr 1.0×/2.0×, regime={regime}", "atr",
                                   1.0, 2.0, (0.5, 0.8, 1.2), regime))))

    # The ablation says the entry threshold carries the edge and the stop should
    # stay tight. Judge those finalists the way the house rules require: both
    # halves of the window, not just the flattering one.
    print("\n" + "=" * 104)
    print("FINALISTS — deep gap + tight ₹30 stop, walk-forward")
    print("=" * 104)
    finals = [
        Variant("LIVE  gap ₹20 / stop ₹30 / trend", "fixed", 20.0, 30.0, LIVE_TARGETS, "trend"),
        Variant("A  gap ₹40 / stop ₹30 / trend", "fixed", 40.0, 30.0, LIVE_TARGETS, "trend"),
        Variant("B  gap ₹40 / stop ₹30 / stabil", "fixed", 40.0, 30.0, LIVE_TARGETS, "stabilization"),
        Variant("C  gap 0.75×ATR / stop ₹30 / trend", "atr", 0.75, 30.0 / 52.57, LIVE_TARGETS, "trend"),
        Variant("D  gap 0.75×ATR / SL 0.57×ATR / stabil", "atr", 0.75, 0.57,
                (0.29, 0.38, 0.57), "stabilization"),
        # The deep-gap/tight-stop pairs above all fail in-sample: a bigger
        # dislocation needs proportionally more room, or the stop knifes the
        # entry before reversion. Pair the deep gap with a WIDE stop instead.
        Variant("E  gap 1.0×ATR / SL 2.0×ATR / trend", "atr", 1.0, 2.0, (0.5, 0.8, 1.2), "trend"),
        Variant("F  gap 1.25×ATR / SL 2.0×ATR / trend", "atr", 1.25, 2.0, (0.5, 0.8, 1.2), "trend"),
        Variant("G  gap 1.0×ATR / SL 2.0×ATR / stabil", "atr", 1.0, 2.0, (0.5, 0.8, 1.2), "stabilization"),
        Variant("H  gap ₹50 / stop ₹105 / trend", "fixed", 50.0, 105.0, (26.0, 42.0, 63.0), "trend"),
    ]
    for label, part in halves.items():
        print(f"\n  {label}")
        for v in finals:
            print(_row(run(part, v)))
    print("\n  FULL WINDOW")
    for v in finals:
        print(_row(run(df, v)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
