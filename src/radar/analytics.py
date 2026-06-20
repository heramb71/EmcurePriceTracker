"""Performance analytics + ranking over recorded outcomes.

Aggregates signals × outcomes (at a chosen horizon) into win-rate, profit factor,
expectancy, and average gain/loss/MFE/MAE — grouped by stock, signal type, or
regime. Rankings pick the best stock/signal/regime by **expectancy** (per the
brief's success metric — not win-rate or alert count).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.radar.alert_format import signal_label
from src.radar.store import HORIZONS

DEFAULT_HORIZON = "5d"
_MIN_SAMPLES = 5  # below this we report "insufficient data" rather than noise


@dataclass(frozen=True)
class GroupStats:
    key: str
    n: int
    wins: int
    losses: int
    neutral: int
    win_rate: float       # over decided (win+loss) signals
    profit_factor: float  # sum gains / |sum losses| (per-share pnl)
    avg_gain: float
    avg_loss: float
    avg_mfe: float
    avg_mae: float
    expectancy: float     # mean per-share pnl across all decided signals


def _joined(conn: sqlite3.Connection, horizon: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.stock, s.signal_type, s.regime, s.price_at_alert,
               o.price, o.mfe, o.mae, o.outcome
        FROM signals s
        JOIN outcomes o ON o.signal_id = s.id
        WHERE o.horizon = ? AND o.outcome IS NOT NULL
        """,
        (horizon,),
    ).fetchall()


def _stats_for(key: str, rows: list[sqlite3.Row]) -> GroupStats:
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    neutral = [r for r in rows if r["outcome"] == "NEUTRAL"]

    def pnl(r) -> float:
        return float(r["price"]) - float(r["price_at_alert"])

    gains = [pnl(r) for r in wins]
    loss_vals = [pnl(r) for r in losses]
    decided = gains + loss_vals

    gross_gain = sum(g for g in gains if g > 0)
    gross_loss = abs(sum(l for l in loss_vals if l < 0))
    pf = round(gross_gain / gross_loss, 2) if gross_loss > 0 else (
        float("inf") if gross_gain > 0 else 0.0
    )
    decided_n = len(gains) + len(loss_vals)
    return GroupStats(
        key=key,
        n=len(rows),
        wins=len(wins),
        losses=len(losses),
        neutral=len(neutral),
        win_rate=round(len(wins) / decided_n * 100, 1) if decided_n else 0.0,
        profit_factor=pf,
        avg_gain=round(sum(gains) / len(gains), 2) if gains else 0.0,
        avg_loss=round(sum(loss_vals) / len(loss_vals), 2) if loss_vals else 0.0,
        avg_mfe=round(sum(float(r["mfe"]) for r in rows) / len(rows), 2) if rows else 0.0,
        avg_mae=round(sum(float(r["mae"]) for r in rows) / len(rows), 2) if rows else 0.0,
        expectancy=round(sum(decided) / decided_n, 2) if decided_n else 0.0,
    )


def summary(
    conn: sqlite3.Connection,
    group_by: str = "stock",
    horizon: str = DEFAULT_HORIZON,
) -> list[GroupStats]:
    """Per-group stats sorted by expectancy (desc). ``group_by`` ∈
    {stock, signal_type, regime}."""
    if group_by not in ("stock", "signal_type", "regime"):
        raise ValueError(f"invalid group_by: {group_by}")
    rows = _joined(conn, horizon)
    buckets: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        buckets.setdefault(r[group_by], []).append(r)
    stats = [_stats_for(k, v) for k, v in buckets.items()]
    return sorted(stats, key=lambda g: g.expectancy, reverse=True)


def _best(conn, group_by, horizon) -> Optional[GroupStats]:
    stats = [g for g in summary(conn, group_by, horizon) if g.n >= _MIN_SAMPLES]
    return stats[0] if stats else None


def best_stock(conn, horizon=DEFAULT_HORIZON):
    return _best(conn, "stock", horizon)


def best_signal(conn, horizon=DEFAULT_HORIZON):
    return _best(conn, "signal_type", horizon)


def best_regime(conn, horizon=DEFAULT_HORIZON):
    return _best(conn, "regime", horizon)


def _format_group(title: str, stats: list[GroupStats], label_fn=lambda k: k) -> list[str]:
    lines = [f"── {title} ──"]
    if not stats:
        lines.append("  (no decided outcomes yet)")
        return lines
    for g in stats:
        pf = "∞" if g.profit_factor == float("inf") else f"{g.profit_factor:.2f}"
        lines.append(
            f"  {label_fn(g.key):<28} n={g.n:<3} WR={g.win_rate:>5.1f}%  "
            f"PF={pf:>5}  E=₹{g.expectancy:>7.2f}  "
            f"MFE={g.avg_mfe:>6.2f} MAE={g.avg_mae:>6.2f}"
        )
    return lines


def format_report(conn: sqlite3.Connection, horizon: str = DEFAULT_HORIZON) -> str:
    """Human-readable analytics dashboard for the CLI / digest."""
    total = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
    decided = len(_joined(conn, horizon))

    out = [
        f"📊 RADAR PERFORMANCE  (horizon={horizon})",
        f"Signals generated: {total}   Decided outcomes @{horizon}: {decided}",
        "",
    ]
    if decided < _MIN_SAMPLES:
        out.append(f"Insufficient data — need ≥{_MIN_SAMPLES} decided outcomes "
                   f"for reliable stats (have {decided}).")
        return "\n".join(out)

    out += _format_group("By stock", summary(conn, "stock", horizon)) + [""]
    out += _format_group("By signal", summary(conn, "signal_type", horizon),
                         label_fn=signal_label) + [""]
    out += _format_group("By regime", summary(conn, "regime", horizon)) + [""]

    bs, bsig, breg = best_stock(conn, horizon), best_signal(conn, horizon), best_regime(conn, horizon)
    out += [
        "── Leaders (by expectancy, n≥%d) ──" % _MIN_SAMPLES,
        f"  Best stock:  {bs.key if bs else '—'}",
        f"  Best signal: {signal_label(bsig.key) if bsig else '—'}",
        f"  Best regime: {breg.key if breg else '—'}",
    ]
    return "\n".join(out)


def available_horizons() -> tuple[str, ...]:
    return HORIZONS
