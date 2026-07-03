"""
Durable P&L ledger — the record that answers "is the live system making money?"

Until now every closed trade lived only in the ephemeral, gitignored
strategy_state.json journal — impossible to query and wiped on any state reset.
This module persists each closed round-trip to a small SQLite file
(``emcure.db``, gitignored), mirroring the radar's store: stdlib ``sqlite3``,
WAL mode, single-writer (the tracker), OCI-free-tier friendly.

Design rules:
- One row per *closed* trade (entry + exit + realized P&L), so win-rate /
  profit-factor / expectancy fall straight out of a GROUP BY.
- ``log_trade`` is resilient: a ledger write must NEVER break trading, so it
  swallows every error and logs it — same contract as the network helpers.
- ``dry_run`` is stored so paper (MANAGED_CYCLE_LIVE=false) and live trades can
  be measured separately.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "emcure.db")


def db_path() -> str:
    """Resolved DB path — ``EMCURE_DB_PATH`` overrides the repo-root default."""
    return os.environ.get("EMCURE_DB_PATH", _DEFAULT_DB)


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection with WAL + row factory, ensuring the schema exists."""
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy    TEXT    NOT NULL,   -- managed | manual
            ticker      TEXT    NOT NULL,
            qty         INTEGER NOT NULL,
            entry_price REAL    NOT NULL,
            exit_price  REAL    NOT NULL,
            pnl         REAL    NOT NULL,   -- realized ₹ (exit-entry)*qty
            exit_reason TEXT    NOT NULL DEFAULT '',  -- target | stop | manual | external
            dry_run     INTEGER NOT NULL DEFAULT 0,
            opened_at   TEXT,
            closed_at   TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
        CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
        """
    )
    conn.commit()


def record_trade(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    ticker: str,
    qty: int,
    entry_price: float,
    exit_price: float,
    pnl: float,
    exit_reason: str = "",
    dry_run: bool = False,
    opened_at: Optional[str] = None,
    closed_at: Optional[datetime] = None,
) -> int:
    """Persist one closed trade; returns its row id."""
    when = (closed_at or datetime.now()).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO trades
            (strategy, ticker, qty, entry_price, exit_price, pnl,
             exit_reason, dry_run, opened_at, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (strategy, ticker, int(qty), float(entry_price), float(exit_price),
         float(pnl), exit_reason, 1 if dry_run else 0, opened_at, when),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_trade(**fields: Any) -> None:
    """Resilient fire-and-forget insert (opens its own connection).

    Exits are rare, so a fresh connection per call is fine and keeps callers
    free of connection plumbing. Never raises — a ledger failure must not break
    the trading loop.
    """
    try:
        conn = connect()
        try:
            record_trade(conn, **fields)
        finally:
            conn.close()
    except Exception:
        logger.exception("ledger.log_trade failed (trade still executed): %s", fields)


# ─────────────────────────────────────────────────────────────────────────────
# Analytics — win-rate / profit-factor / expectancy
# ─────────────────────────────────────────────────────────────────────────────

def summary(conn: sqlite3.Connection, *, strategy: Optional[str] = None,
            include_dry_run: bool = True) -> dict[str, Any]:
    """Aggregate stats over closed trades, optionally filtered by strategy."""
    where = []
    params: list[Any] = []
    if strategy is not None:
        where.append("strategy = ?")
        params.append(strategy)
    if not include_dry_run:
        where.append("dry_run = 0")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT pnl FROM trades {clause}", params).fetchall()
    pnls = [r["pnl"] for r in rows]
    return _stats(pnls)


def _stats(pnls: list[float]) -> dict[str, Any]:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # positive magnitude
    total = sum(pnls)
    win_rate = (len(wins) / n * 100) if n else 0.0
    # PF = gross profit / gross loss; infinite when there are wins but no losses.
    if gross_loss > 0:
        profit_factor: Optional[float] = round(gross_profit / gross_loss, 2)
    else:
        profit_factor = None  # undefined / no losing trades
    expectancy = round(total / n, 2) if n else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "total_pnl": round(total, 2),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
    }


def day_stats(day: str) -> dict[str, Any]:
    """Realized P&L (₹) and closed-trade count for one date (``YYYY-MM-DD``),
    live trades only — dry-run rows are paper, not money. Feeds the EOD
    summary's "Day P&L / trades today" line. Never raises: on any failure it
    reports zeros, mirroring ``log_trade``'s must-not-break contract."""
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS pnl, COUNT(*) AS trades "
                "FROM trades WHERE dry_run = 0 AND date(closed_at) = ?",
                (day,),
            ).fetchone()
            return {"pnl": round(float(row["pnl"]), 2), "trades": int(row["trades"])}
        finally:
            conn.close()
    except Exception:
        logger.exception("ledger.day_stats failed for %s", day)
        return {"pnl": 0.0, "trades": 0}


def recent_trades(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    """Most recently closed trades, newest first."""
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)
    ).fetchall()
    return [dict(r) for r in rows]


def by_strategy(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per-strategy summary blocks."""
    strategies = [r["strategy"] for r in
                  conn.execute("SELECT DISTINCT strategy FROM trades").fetchall()]
    return {s: summary(conn, strategy=s) for s in sorted(strategies)}


def format_report(conn: Optional[sqlite3.Connection] = None) -> str:
    """Human-readable P&L report for `python -m apps.trade report`."""
    own = conn is None
    conn = conn or connect()
    try:
        overall = summary(conn)
        if overall["trades"] == 0:
            return "📒 P&L ledger is empty — no closed trades recorded yet."
        lines = ["📒 *EMCURE P&L Ledger*", ""]
        lines += _format_block("All trades", overall)
        per = by_strategy(conn)
        for name, block in per.items():
            lines.append("")
            lines += _format_block(name, block)
        return "\n".join(lines)
    finally:
        if own:
            conn.close()


def _format_block(title: str, s: dict[str, Any]) -> list[str]:
    pf = "∞" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    return [
        f"— {title} —",
        f"  Trades: {s['trades']}  ·  Win rate: {s['win_rate']:.0f}%  "
        f"({s['wins']}W / {s['losses']}L)",
        f"  Total P&L: ₹{s['total_pnl']:+,.0f}  ·  Expectancy: ₹{s['expectancy']:+,.0f}/trade",
        f"  Profit factor: {pf}  ·  Gross +₹{s['gross_profit']:,.0f} / −₹{s['gross_loss']:,.0f}",
    ]
