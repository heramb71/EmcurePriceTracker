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
