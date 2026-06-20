"""SQLite persistence for the radar — signals + forward outcomes.

A single file (``radar.db``, gitignored) holds every fired signal and its
re-evaluated outcomes at each horizon. Stdlib ``sqlite3`` only — no server, no
new dependency, OCI-free-tier friendly. WAL mode keeps the CLI readable while
the service writes; the radar is the sole writer so there is no contention.

The store is intentionally thin: schema + typed insert/read helpers. Analytics
SELECTs live in ``analytics.py``; this module just exposes a connection factory.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

# Outcome horizons, in order, with their elapsed-time windows.
HORIZONS: tuple[str, ...] = ("1h", "4h", "1d", "3d", "5d", "10d")
_HORIZON_DELTAS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "5d": timedelta(days=5),
    "10d": timedelta(days=10),
}

_DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "..", "radar.db")


def db_path() -> str:
    """Resolved DB path — ``RADAR_DB_PATH`` overrides the repo-root default."""
    return os.environ.get("RADAR_DB_PATH", _DEFAULT_DB)


def horizon_delta(horizon: str) -> timedelta:
    return _HORIZON_DELTAS[horizon]


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection with WAL + row factory, ensuring the schema exists."""
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT    NOT NULL,
            stock            TEXT    NOT NULL,
            signal_type      TEXT    NOT NULL,
            confidence       INTEGER NOT NULL,
            regime           TEXT    NOT NULL,
            price_at_alert   REAL    NOT NULL,
            suggested_stop   REAL    NOT NULL,
            suggested_target REAL    NOT NULL,
            rr               REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            signal_id    INTEGER NOT NULL,
            horizon      TEXT    NOT NULL,
            price        REAL,
            mfe          REAL,
            mae          REAL,
            outcome      TEXT,
            evaluated_at TEXT    NOT NULL,
            PRIMARY KEY (signal_id, horizon),
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
        CREATE INDEX IF NOT EXISTS idx_outcomes_horizon ON outcomes(horizon);
        """
    )
    conn.commit()


def insert_signal(
    conn: sqlite3.Connection,
    *,
    stock: str,
    signal_type: str,
    confidence: int,
    regime: str,
    price_at_alert: float,
    suggested_stop: float,
    suggested_target: float,
    rr: float,
    ts: Optional[datetime] = None,
) -> int:
    """Persist a fired signal; returns its row id."""
    when = (ts or datetime.now()).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO signals
            (ts, stock, signal_type, confidence, regime,
             price_at_alert, suggested_stop, suggested_target, rr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (when, stock, signal_type, int(confidence), regime,
         float(price_at_alert), float(suggested_stop),
         float(suggested_target), float(rr)),
    )
    conn.commit()
    return int(cur.lastrowid)


def due_outcomes(
    conn: sqlite3.Connection, now: Optional[datetime] = None
) -> list[dict]:
    """Return signal/horizon pairs whose window has matured but is unrecorded.

    Each item: ``{signal_id, stock, signal_type, ts, price_at_alert,
    suggested_stop, suggested_target, horizon}``. A horizon is *due* when
    ``now >= ts + delta`` and no ``outcomes`` row exists for that pair yet.
    """
    now = now or datetime.now()
    rows = conn.execute("SELECT * FROM signals").fetchall()
    recorded = {
        (r["signal_id"], r["horizon"])
        for r in conn.execute(
            "SELECT signal_id, horizon FROM outcomes"
        ).fetchall()
    }
    due: list[dict] = []
    for sig in rows:
        ts = datetime.fromisoformat(sig["ts"])
        for horizon in HORIZONS:
            if (sig["id"], horizon) in recorded:
                continue
            if now >= ts + _HORIZON_DELTAS[horizon]:
                due.append(
                    {
                        "signal_id": sig["id"],
                        "stock": sig["stock"],
                        "signal_type": sig["signal_type"],
                        "ts": ts,
                        "price_at_alert": sig["price_at_alert"],
                        "suggested_stop": sig["suggested_stop"],
                        "suggested_target": sig["suggested_target"],
                        "horizon": horizon,
                    }
                )
    return due


def record_outcome(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    horizon: str,
    price: Optional[float],
    mfe: Optional[float],
    mae: Optional[float],
    outcome: Optional[str],
    evaluated_at: Optional[datetime] = None,
) -> None:
    """Upsert a horizon outcome (idempotent via the composite PK)."""
    when = (evaluated_at or datetime.now()).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO outcomes
            (signal_id, horizon, price, mfe, mae, outcome, evaluated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id, horizon) DO UPDATE SET
            price=excluded.price, mfe=excluded.mfe, mae=excluded.mae,
            outcome=excluded.outcome, evaluated_at=excluded.evaluated_at
        """,
        (signal_id, horizon, price, mfe, mae, outcome, when),
    )
    conn.commit()
