"""Tests for the shared bot command handlers (src/emcure/commands.py) —
previously untestable inside bot_server's Flask module."""
from __future__ import annotations

import pytest

import src.emcure.commands as commands
import src.emcure.managed_cycle as mc
import src.emcure.trade_manager as tm
from src.emcure import ledger
from src.shared.costs import round_trip_charges


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "_STATE_FILE", str(tmp_path / "trade_state.json"))
    monkeypatch.setattr(mc, "_STATE_FILE", str(tmp_path / "managed_state.json"))
    monkeypatch.setenv("EMCURE_DB_PATH", str(tmp_path / "emcure.db"))
    monkeypatch.setenv("CAPITAL", "100000")
    monkeypatch.setenv("RISK_RUPEES", "4500")


# ── BUY validation ────────────────────────────────────────────────────────────

def test_buy_rejects_zero_and_negative_price():
    assert "positive" in commands.handle_buy(["BUY", "0"])
    assert "positive" in commands.handle_buy(["BUY", "-5"])
    assert tm.get_trade() is None


def test_buy_rejects_price_above_capital_without_qty():
    reply = commands.handle_buy(["BUY", "150000"])
    assert "Qty works out to 0" in reply
    assert tm.get_trade() is None


def test_buy_records_trade_with_auto_qty():
    reply = commands.handle_buy(["BUY", "1693"])
    assert "Trade recorded" in reply
    trade = tm.get_trade()
    assert trade["entry"] == 1693.0 and trade["qty"] == int(100000 / 1693)


# ── SELL: never close blind, always net of charges ────────────────────────────

def test_sell_refuses_without_a_price(monkeypatch):
    commands.handle_buy(["BUY", "1693", "8"])
    monkeypatch.setattr(commands, "live_price", lambda: 0.0)
    reply = commands.handle_sell(["SELL"])
    assert "NOT closed" in reply
    assert tm.get_trade() is not None          # trade survives the failed fetch


def test_sell_with_explicit_price_logs_net_ledger_row():
    commands.handle_buy(["BUY", "1700", "8"])
    reply = commands.handle_sell(["SELL", "1720"])
    assert "Net P&L" in reply and "Charges" in reply
    assert tm.get_trade() is None

    row = ledger.recent_trades(ledger.connect(), limit=1)[0]
    charges = round_trip_charges(1700.0, 1720.0, 8)
    assert row["pnl"] == 160.0
    assert row["charges"] == charges
    assert row["net_pnl"] == round(160.0 - charges, 2)


def test_sell_rejects_garbage_price():
    commands.handle_buy(["BUY", "1700", "8"])
    assert "Invalid price" in commands.handle_sell(["SELL", "ABC"])
    assert tm.get_trade() is not None


# ── EXIT / HALT / RESUME (managed-cycle remote control) ───────────────────────

def test_exit_requires_managed_cycle(monkeypatch):
    monkeypatch.setenv("MANAGED_CYCLE", "false")
    assert "not enabled" in commands.handle_exit(["EXIT"])


def test_exit_flat_vs_holding(monkeypatch):
    monkeypatch.setenv("MANAGED_CYCLE", "true")
    assert "flat" in commands.handle_exit(["EXIT"])

    cfg = mc.ManagedConfig.from_env()
    mc.set_position(1700.0, 8, cfg)
    reply = commands.handle_exit(["EXIT"])
    assert "EXIT queued" in reply
    assert mc._load()["exit_requested"] is True


def test_halt_and_resume_toggle_the_flag(monkeypatch):
    monkeypatch.setenv("MANAGED_CYCLE", "true")
    assert "HALTED" in commands.handle_halt(["HALT"])
    assert mc.is_halted()
    assert "resumed" in commands.handle_resume(["RESUME"])
    assert not mc.is_halted()


# ── STATUS + live price ───────────────────────────────────────────────────────

def test_status_reports_managed_flat_and_manual_trade(monkeypatch):
    monkeypatch.setenv("MANAGED_CYCLE", "true")
    monkeypatch.setattr(commands, "live_price", lambda: 1700.0)
    commands.handle_buy(["BUY", "1690", "8"])
    reply = commands.handle_status(["STATUS"])
    assert "Managed cycle — flat" in reply
    assert "Manual Trade Position" in reply


def test_live_price_prefers_kite_ltp(monkeypatch):
    class _B:
        def get_ltp(self, ticker):
            return 1234.5
    monkeypatch.setattr(commands, "_kite_broker", lambda: _B())
    assert commands.live_price() == 1234.5


def test_registry_has_all_commands():
    assert set(commands.HANDLERS) == {
        "BUY", "SELL", "STATUS", "EXIT", "HALT", "RESUME",
        "CRYPTO", "HELP", "TOKEN", "KITE",
    }
