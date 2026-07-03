"""
Forward-outcome tracking for crypto alerts — the radar pattern applied to
BTC/ETH.

The crypto service has sent oversold/overbought/Strong-Buy alerts for months
with zero record of whether they were RIGHT. This module records every fired
alert to SQLite (``crypto.db``, WAL, gitignored — same shape as radar.db) and,
once each horizon matures, books the signed forward move so expectancy falls
out of a GROUP BY. Zero risk: it never trades and never alerts; it only turns
the existing alert stream into evidence.

Direction per alert type: Strong Buy / oversold expect UP (+1); Strong Sell /
overbought expect DOWN (−1). ``signed_pct`` = direction × move, so a positive
number always means "the alert was right".

Verdict thresholds scale with horizon (crypto's daily σ is ~2.5–3%):
WIN when signed_pct ≥ +threshold, LOSS ≤ −threshold, else NEUTRAL.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "crypto.db")

# horizon label → (maturity, WIN/LOSS threshold on the signed move, %)
HORIZONS: dict[str, tuple[timedelta, float]] = {
    "1d": (timedelta(days=1), 1.5),
    "3d": (timedelta(days=3), 3.0),
    "7d": (timedelta(days=7), 5.0),
}


def db_path() -> str:
    return os.environ.get("CRYPTO_DB_PATH", _DEFAULT_DB)


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL,           -- alert time (ISO, IST)
            symbol     TEXT    NOT NULL,           -- BTC | ETH
            alert_type TEXT    NOT NULL,           -- strong_buy|strong_sell|oversold|overbought
            direction  INTEGER NOT NULL,           -- +1 expects up, -1 expects down
            signal     TEXT    NOT NULL,           -- score label at alert time
            score      REAL    NOT NULL,
            rsi        REAL    NOT NULL,
            price_usd  REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id    INTEGER NOT NULL REFERENCES signals(id),
            horizon      TEXT    NOT NULL,
            price_usd    REAL    NOT NULL,         -- price at evaluation
            signed_pct   REAL    NOT NULL,         -- direction × move (+ = right)
            outcome      TEXT    NOT NULL,         -- WIN | LOSS | NEUTRAL
            evaluated_at TEXT    NOT NULL,
            UNIQUE (signal_id, horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
        """
    )
    conn.commit()
    return conn


def classify_alert(sig: dict) -> Optional[tuple[str, int]]:
    """(alert_type, direction) for an alert-worthy signal dict, or None.
    Mirrors signals.is_alert_worthy's triggers exactly."""
    if sig["signal"] == "Strong Buy":
        return "strong_buy", +1
    if sig["signal"] == "Strong Sell":
        return "strong_sell", -1
    if sig["rsi"] < 35:
        return "oversold", +1
    if sig["rsi"] > 68:
        return "overbought", -1
    return None


def record_alert(conn: sqlite3.Connection, symbol: str, sig: dict, quote: dict,
                 now: datetime) -> Optional[int]:
    """Persist one fired alert. Never raises — tracking must not break alerting."""
    try:
        kind = classify_alert(sig)
        if kind is None:
            return None
        alert_type, direction = kind
        cur = conn.execute(
            "INSERT INTO signals (ts, symbol, alert_type, direction, signal, score, rsi, price_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), symbol, alert_type, direction,
             sig["signal"], float(sig["score"]), float(sig["rsi"]),
             float(quote["price_usd"])),
        )
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        logger.exception("crypto outcomes: record_alert failed for %s", symbol)
        return None


def evaluate_due(conn: sqlite3.Connection, prices_usd: dict[str, float],
                 now: datetime) -> int:
    """Book outcomes for every (signal, horizon) that has matured and has a
    live price for its symbol. The loop runs every ~10 min, so evaluation lands
    within minutes of maturity. Returns rows written. Never raises."""
    written = 0
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.ts, s.symbol, s.direction, s.price_usd
            FROM signals s
            WHERE (SELECT COUNT(*) FROM outcomes o WHERE o.signal_id = s.id)
                  < ?
            """,
            (len(HORIZONS),),
        ).fetchall()
        for r in rows:
            price_now = prices_usd.get(r["symbol"])
            if not price_now or r["price_usd"] <= 0:
                continue
            alert_ts = datetime.fromisoformat(r["ts"])
            for horizon, (delta, threshold) in HORIZONS.items():
                if now < alert_ts + delta:
                    continue
                move_pct = (price_now - r["price_usd"]) / r["price_usd"] * 100
                signed = round(r["direction"] * move_pct, 3)
                outcome = ("WIN" if signed >= threshold
                           else "LOSS" if signed <= -threshold else "NEUTRAL")
                cur = conn.execute(
                    "INSERT OR IGNORE INTO outcomes "
                    "(signal_id, horizon, price_usd, signed_pct, outcome, evaluated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (r["id"], horizon, float(price_now), signed, outcome,
                     now.isoformat(timespec="seconds")),
                )
                written += cur.rowcount
        conn.commit()
    except Exception:
        logger.exception("crypto outcomes: evaluate_due failed")
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────────────────────────────────────

def summary(conn: sqlite3.Connection, horizon: str = "3d") -> list[dict[str, Any]]:
    """Per (symbol, alert_type) blocks at one horizon, sorted by expectancy."""
    rows = conn.execute(
        """
        SELECT s.symbol, s.alert_type, o.signed_pct, o.outcome
        FROM signals s JOIN outcomes o ON o.signal_id = s.id
        WHERE o.horizon = ?
        """,
        (horizon,),
    ).fetchall()
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        buckets.setdefault((r["symbol"], r["alert_type"]), []).append(r)
    out = []
    for (symbol, alert_type), rs in buckets.items():
        wins = sum(1 for r in rs if r["outcome"] == "WIN")
        losses = sum(1 for r in rs if r["outcome"] == "LOSS")
        decided = wins + losses
        out.append({
            "symbol": symbol,
            "alert_type": alert_type,
            "n": len(rs),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
            "expectancy_pct": round(sum(r["signed_pct"] for r in rs) / len(rs), 2),
        })
    return sorted(out, key=lambda b: b["expectancy_pct"], reverse=True)


def format_report(conn: sqlite3.Connection) -> str:
    """Human-readable forward-outcome report across all horizons."""
    total = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
    lines = [f"📊 CRYPTO ALERT OUTCOMES  (signals recorded: {total})"]
    if total == 0:
        lines.append("No alerts recorded yet — the tracker fills as alerts fire.")
        return "\n".join(lines)
    for horizon in HORIZONS:
        blocks = summary(conn, horizon)
        lines.append(f"\n── horizon {horizon} "
                     f"(WIN/LOSS at ±{HORIZONS[horizon][1]:.1f}%) ──")
        if not blocks:
            lines.append("  (no matured outcomes yet)")
            continue
        for b in blocks:
            lines.append(
                f"  {b['symbol']:<4} {b['alert_type']:<12} n={b['n']:<3} "
                f"WR={b['win_rate']:>5.1f}%  E={b['expectancy_pct']:+.2f}%"
            )
    lines.append("\n+E = the alert's direction was right on average. Judge combos")
    lines.append("only at n≥20, same discipline as the radar.")
    return "\n".join(lines)
