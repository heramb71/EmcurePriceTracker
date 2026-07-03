"""Aggregate backtest of the radar over a longer window (research only).

Replays the exact production engine hour-by-hour with no look-ahead (reusing
``radar_replay.snapshot_at``), applies the live dispatch logic (per-family gate +
cooldown + daily budget via ``AlertGate``) so repeated hourly prints collapse into
realistic *distinct* alerts, then evaluates each alert's forward outcome on the
next N daily bars (target-before-stop) and aggregates win-rate / profit-factor /
expectancy by signal, stock, and regime.

A round-trip cost haircut is applied to net expectancy because — per the repo's
own swing-lab finding — cost drag is what kills thin edges.

Run:  python -m apps.radar_backtest [months] [min_adtv_cr]   (default 6 months, 100)
"""
from __future__ import annotations

import sys
from collections import defaultdict

import pandas as pd

from apps.radar_replay import _daily_slice, _intraday, snapshot_at
from src.radar import scoring, signals, tracker
from src.radar.alert_format import signal_label
from src.radar.dispatch import AlertGate
from src.radar.features import fetch_index_daily
from src.radar.regime import breadth, current_regime
from src.radar.scan import _core_symbols
from src.radar.universe import NIFTY, SYMBOLS
from src.shared.data import fetch_daily

FORWARD_DAYS = 10            # evaluate target-vs-stop over the next N daily bars
ROUND_TRIP_COST_PCT = 0.4   # delivery round-trip haircut on net expectancy


def _forward_outcome(daily_full, day, entry, stop, target):
    """Outcome over the next FORWARD_DAYS daily bars *after* the alert day
    (no look-ahead into the alert day itself). Returns (outcome, exit_pnl_pct)."""
    fwd = daily_full[pd.to_datetime(daily_full["date"]).dt.date > day].head(FORWARD_DAYS)
    bars = [{"high": float(r.high), "low": float(r.low), "close": float(r.close)}
            for r in fwd.itertuples()]
    _, _, _, outcome = tracker.evaluate_window(entry, stop, target, bars)
    if outcome == "WIN":
        exit_px = target
    elif outcome == "LOSS":
        exit_px = stop
    elif outcome == "NEUTRAL":
        exit_px = bars[-1]["close"]
    else:
        return None, None
    return outcome, (exit_px - entry) / entry * 100.0


def main() -> None:
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    min_adtv = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    intraday_days = months * 31

    print(f"Loading {months}mo data… (universe={','.join(SYMBOLS)})")
    daily = {s: fetch_daily(s, days=intraday_days + 220) for s in SYMBOLS}
    intra = {s: _intraday(s, days=intraday_days) for s in SYMBOLS}
    nifty_daily = fetch_index_daily(NIFTY, days=intraday_days + 220)
    core = _core_symbols()

    any_intra = next(d for d in intra.values() if d is not None)
    days = sorted(set(any_intra["d"]))
    print(f"Window: {days[0]} → {days[-1]}  ({len(days)} sessions, "
          f"gates mom={scoring.momentum_gate()}/rev={scoring.reversion_gate()}, "
          f"{min_adtv:.0f}Cr floor, core={','.join(sorted(core))})\n")

    gate = AlertGate(max_per_day=int(1e9), cooldown_minutes=90)  # budget off; cooldown on
    records: list[dict] = []

    for day in days:
        nifty_slice = _daily_slice(nifty_daily, day)
        above = []
        for s in SYMBOLS:
            ds = _daily_slice(daily[s], day) if daily[s] is not None else None
            if ds is not None and len(ds) >= 50:
                above.append(float(ds["close"].iloc[-1]) > float(ds["close"].tail(50).mean()))
        if nifty_slice is None or len(nifty_slice) < 60:
            continue
        regime = current_regime(nifty_slice, breadth(above))

        day_bars = {s: (intra[s][intra[s]["d"] == day].reset_index(drop=True)
                        if intra[s] is not None else pd.DataFrame()) for s in SYMBOLS}
        n_bars = max((len(b) for b in day_bars.values()), default=0)

        for h in range(n_bars):
            scored = []
            ts = None
            for s in SYMBOLS:
                b = day_bars[s]
                if h >= len(b):
                    continue
                ts = b["dt"].iloc[h]
                snap = snapshot_at(s, daily[s], b, day, h, nifty_slice)
                if snap is None:
                    continue
                if s.upper() not in core and snap.adtv_cr < min_adtv:
                    continue
                for hit in signals.detect(snap, regime):
                    conf = scoring.confidence(snap, hit, regime)
                    if scoring.passes_gate(hit.signal_type, conf):
                        scored.append((hit, conf, snap.price))
            if not scored:
                continue
            ranked = scoring.rank([(h_, c_) for h_, c_, _ in scored])
            price_by_id = {id(h_): p_ for h_, _, p_ in scored}
            individual, _ = gate.select(ranked, ts)
            for hit, conf, _rank in individual:
                entry = price_by_id[id(hit)]
                outcome, pnl = _forward_outcome(daily[hit.stock], day, entry, hit.stop, hit.target)
                if outcome is None:
                    continue
                records.append({
                    "stock": hit.stock, "sig": hit.signal_type, "regime": regime,
                    "conf": conf, "outcome": outcome, "pnl": pnl, "rr": hit.rr,
                })

    _report(records)


def _stats(rows: list[dict]) -> dict:
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    decided = wins + losses
    gross_gain = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    pf = (gross_gain / gross_loss) if gross_loss else (float("inf") if gross_gain else 0.0)
    exp = (sum(r["pnl"] for r in decided) / len(decided)) if decided else 0.0
    return {
        "n": len(rows), "w": len(wins), "l": len(losses),
        "neu": len(rows) - len(decided),
        "wr": (len(wins) / len(decided) * 100) if decided else 0.0,
        "pf": pf, "exp": exp, "net": exp - ROUND_TRIP_COST_PCT,
    }


def _line(label: str, st: dict) -> str:
    pf = "∞" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    return (f"  {label:<26} n={st['n']:>3}  W/L={st['w']}/{st['l']}  "
            f"WR={st['wr']:>5.1f}%  PF={pf:>5}  "
            f"grossE={st['exp']:>+5.2f}%  netE={st['net']:>+5.2f}%")


def _group(title, rows, key, label_fn=lambda k: k):
    buckets = defaultdict(list)
    for r in rows:
        buckets[r[key]].append(r)
    out = [f"── {title} ──"]
    for k, v in sorted(buckets.items(), key=lambda kv: _stats(kv[1])["exp"], reverse=True):
        out.append(_line(label_fn(k), _stats(v)))
    return out


def _report(records: list[dict]) -> None:
    print("═" * 78)
    if not records:
        print("No gated alerts over the window.")
        return
    print(_line("OVERALL", _stats(records)).strip())
    print(f"(net expectancy = gross − {ROUND_TRIP_COST_PCT}% round-trip cost; "
          f"forward window = {FORWARD_DAYS} trading days)\n")
    for ln in _group("By signal", records, "sig", signal_label):
        print(ln)
    print()
    for ln in _group("By stock", records, "stock"):
        print(ln)
    print()
    for ln in _group("By regime", records, "regime"):
        print(ln)


if __name__ == "__main__":
    main()
