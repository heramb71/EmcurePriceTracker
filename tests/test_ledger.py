"""Tests for the durable P&L ledger and its analytics."""
from __future__ import annotations

from src.emcure import ledger


def _conn(tmp_path):
    return ledger.connect(str(tmp_path / "emcure.db"))


def test_record_and_summary_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160, exit_reason="target")
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1570, pnl=-240, exit_reason="stop")
    s = ledger.summary(conn)
    assert s["trades"] == 2
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 50.0
    assert s["total_pnl"] == -80
    assert s["gross_profit"] == 160 and s["gross_loss"] == 240
    assert s["profit_factor"] == round(160 / 240, 2)
    assert s["expectancy"] == -40


def test_profit_factor_none_when_no_losses(tmp_path):
    conn = _conn(tmp_path)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160)
    assert ledger.summary(conn)["profit_factor"] is None


def test_empty_ledger_summary(tmp_path):
    assert ledger.summary(_conn(tmp_path))["trades"] == 0


def test_by_strategy_splits(tmp_path):
    conn = _conn(tmp_path)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160)
    ledger.record_trade(conn, strategy="manual", ticker="EMCURE", qty=10,
                        entry_price=1600, exit_price=1590, pnl=-100)
    per = ledger.by_strategy(conn)
    assert set(per) == {"managed", "manual"}
    assert per["managed"]["total_pnl"] == 160
    assert per["manual"]["total_pnl"] == -100


def test_dry_run_filter(tmp_path):
    conn = _conn(tmp_path)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160, dry_run=True)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1610, pnl=80, dry_run=False)
    assert ledger.summary(conn, include_dry_run=True)["trades"] == 2
    assert ledger.summary(conn, include_dry_run=False)["trades"] == 1


def test_log_trade_never_raises_on_bad_input(monkeypatch, tmp_path):
    # Point at a valid db but pass an unknown kwarg → record_trade raises inside,
    # log_trade must swallow it (a ledger failure can't break trading).
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    ledger.log_trade(strategy="managed", not_a_column="boom")  # must not raise


def test_format_report_empty(tmp_path):
    assert "empty" in ledger.format_report(_conn(tmp_path)).lower()


def test_format_report_has_stats(tmp_path):
    conn = _conn(tmp_path)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160)
    report = ledger.format_report(conn)
    assert "Win rate" in report and "Profit factor" in report


def test_day_stats_sums_live_trades_for_the_date(monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    conn = ledger.connect()
    today = datetime(2026, 7, 3, 15, 10)
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160,
                        closed_at=today)
    ledger.record_trade(conn, strategy="manual", ticker="EMCURE", qty=10,
                        entry_price=1600, exit_price=1590, pnl=-100,
                        closed_at=today)
    # Dry-run rows are paper, not money — excluded from the day tally.
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1700, pnl=800,
                        dry_run=True, closed_at=today)
    # A different day never leaks in.
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1650, pnl=400,
                        closed_at=datetime(2026, 7, 2, 15, 10))
    conn.close()
    stats = ledger.day_stats("2026-07-03")
    assert stats == {"pnl": 60.0, "trades": 2}


def test_day_stats_never_raises(monkeypatch):
    monkeypatch.setenv("EMCURE_DB_PATH", "/nonexistent-dir/emcure.db")
    assert ledger.day_stats("2026-07-03") == {"pnl": 0.0, "trades": 0}


def test_net_pnl_recorded_and_used_by_summary(tmp_path):
    conn = _conn(tmp_path)
    # Gross +160 with ₹47 charges → net 113 is what the analytics must see.
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160, charges=47)
    s = ledger.summary(conn)
    assert s["total_pnl"] == 113.0
    row = ledger.recent_trades(conn, limit=1)[0]
    assert row["charges"] == 47 and row["net_pnl"] == 113.0


def test_pre_charges_db_migrates_and_falls_back_to_gross(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL, ticker TEXT NOT NULL, qty INTEGER NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL NOT NULL, pnl REAL NOT NULL,
            exit_reason TEXT NOT NULL DEFAULT '', dry_run INTEGER NOT NULL DEFAULT 0,
            opened_at TEXT, closed_at TEXT NOT NULL)"""
    )
    old.execute(
        "INSERT INTO trades (strategy, ticker, qty, entry_price, exit_price, pnl, closed_at) "
        "VALUES ('managed', 'EMCURE', 8, 1600, 1620, 160, '2026-07-01T15:00:00')"
    )
    old.commit()
    old.close()

    conn = ledger.connect(path)          # migration runs here
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    assert {"charges", "net_pnl"} <= cols
    # Old row has net_pnl NULL → summary falls back to gross.
    assert ledger.summary(conn)["total_pnl"] == 160.0


def test_week_stats_splits_live_and_dry_within_window(monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    conn = ledger.connect()
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160, charges=40,
                        closed_at=datetime(2026, 7, 1, 15, 0))
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1700, pnl=800, dry_run=True,
                        closed_at=datetime(2026, 7, 2, 15, 0))
    # Outside the 7-day window ending 2026-07-03 — must not leak in.
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1650, pnl=400,
                        closed_at=datetime(2026, 6, 20, 15, 0))
    conn.close()

    wk = ledger.week_stats("2026-07-03")
    assert wk["live"]["trades"] == 1 and wk["live"]["total_pnl"] == 120.0  # net
    assert wk["dry"]["trades"] == 1 and wk["dry"]["total_pnl"] == 800.0


def test_weekly_digest_none_when_empty_and_formats_when_not(monkeypatch, tmp_path):
    from datetime import datetime
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    assert ledger.format_weekly_digest("2026-07-03") is None

    conn = ledger.connect()
    ledger.record_trade(conn, strategy="managed", ticker="EMCURE", qty=8,
                        entry_price=1600, exit_price=1620, pnl=160, charges=40,
                        closed_at=datetime(2026, 7, 1, 15, 0))
    conn.close()
    msg = ledger.format_weekly_digest("2026-07-03")
    assert msg is not None
    assert "Weekly P&L" in msg and "net of charges" in msg
    assert "Since inception" in msg
