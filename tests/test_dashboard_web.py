"""Tests for the read-only web dashboard renderer (pure HTML generation)."""
from __future__ import annotations

from src.emcure.dashboard_web import render_dashboard

_EMPTY_SUMMARY = {"trades": 0}


def _ctx(**over):
    base = {
        "ticker": "EMCURE",
        "now": "2026-07-03 11:00 IST",
        "market_open": True,
        "heartbeat_age": 30,
        "position": None,
        "summary": _EMPTY_SUMMARY,
        "by_strategy": {},
        "recent_trades": [],
    }
    base.update(over)
    return base


def test_renders_valid_html_shell():
    out = render_dashboard(_ctx())
    assert out.startswith("<!DOCTYPE html>")
    assert "EMCURE" in out
    assert "auto-refresh" in out


def test_healthy_heartbeat_shows_live_pill():
    out = render_dashboard(_ctx(heartbeat_age=30, market_open=True))
    assert "pill live" in out


def test_stale_heartbeat_during_market_shows_down():
    out = render_dashboard(_ctx(heartbeat_age=20 * 60, market_open=True))
    assert "pill down" in out
    assert "STALE" in out


def test_missing_heartbeat_shows_no_heartbeat():
    out = render_dashboard(_ctx(heartbeat_age=None))
    assert "NO HEARTBEAT" in out


def test_stale_outside_market_is_not_down():
    # 20 min old but market closed → idle, not an alarm.
    out = render_dashboard(_ctx(heartbeat_age=20 * 60, market_open=False))
    assert "pill down" not in out


def test_position_card_renders_pnl():
    out = render_dashboard(_ctx(position={
        "source": "managed", "entry": 1600, "qty": 8, "price": 1620, "pnl": 160}))
    assert "160" in out and "class=\"up\"" in out


def test_negative_pnl_gets_down_class():
    out = render_dashboard(_ctx(position={
        "source": "manual", "entry": 1600, "qty": 8, "price": 1580, "pnl": -160}))
    assert "class=\"down\"" in out


def test_summary_and_recent_trades_render():
    ctx = _ctx(
        summary={"trades": 2, "wins": 1, "losses": 1, "win_rate": 50.0,
                 "total_pnl": -80, "expectancy": -40, "profit_factor": 0.67,
                 "gross_profit": 160, "gross_loss": 240},
        recent_trades=[{"closed_at": "2026-07-03T14:00", "strategy": "managed",
                        "qty": 8, "entry_price": 1600, "exit_price": 1620, "pnl": 160}],
    )
    out = render_dashboard(ctx)
    assert "Recent trades" in out
    assert "Win rate" in out


def test_html_escaping_of_ticker():
    out = render_dashboard(_ctx(ticker="<script>"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
