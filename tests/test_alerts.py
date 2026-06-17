"""Tests for the alert send functions in src/alerts.py.

These mock the network layer (requests / twilio) so nothing is sent. They pin
the contract every sender shares: return True only on success, False on any
failure, and never raise.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

import src.alerts as alerts
from src.alerts import send_alert, send_whatsapp_alert


# ── Telegram ─────────────────────────────────────────────────────────────────

def test_send_alert_telegram_success():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        assert send_alert("token", "123", "msg") is True
        assert mock_post.call_count == 1


def test_send_alert_markdown_fallback_to_plain():
    # 400 on the Markdown attempt → retry as plain text → 200.
    with patch("requests.post") as mock_post:
        mock_post.side_effect = [MagicMock(status_code=400), MagicMock(status_code=200)]
        assert send_alert("token", "123", "stray * markdown") is True
        assert mock_post.call_count == 2
        # The retry must drop parse_mode.
        assert "parse_mode" not in mock_post.call_args_list[1].kwargs["json"]


def test_send_alert_exception_returns_false():
    with patch("requests.post", side_effect=TimeoutError("slow")):
        assert send_alert("token", "123", "msg") is False


def test_send_alert_unreachable_opens_circuit_breaker():
    """A network-unreachable failure (e.g. the India Telegram block) must open the
    breaker so repeat dispatches skip the dead endpoint instead of timing out
    every time. The breaker auto-closes after the cooldown."""
    alerts._tg_paused_until = 0.0
    try:
        with patch(
            "requests.post",
            side_effect=requests.exceptions.ConnectionError("Network is unreachable"),
        ) as mock_post:
            assert send_alert("token", "123", "msg") is False   # opens breaker
            assert send_alert("token", "123", "msg") is False   # skipped fast
            assert send_alert("token", "123", "msg") is False   # skipped fast
            assert mock_post.call_count == 1                    # only the first hit the network
    finally:
        alerts._tg_paused_until = 0.0   # never leak breaker state to other tests


# ── WhatsApp (Twilio) ────────────────────────────────────────────────────────

def test_send_whatsapp_success():
    with patch("twilio.rest.Client") as mock_client:
        mock_client.return_value.messages.create.return_value = MagicMock(sid="SM1")
        ok = send_whatsapp_alert("sid", "tok", "+1", "+2", "msg")
    assert ok is True


def test_send_whatsapp_exception_returns_false():
    with patch("twilio.rest.Client") as mock_client:
        mock_client.return_value.messages.create.side_effect = Exception("twilio down")
        assert send_whatsapp_alert("sid", "tok", "+1", "+2", "msg") is False
