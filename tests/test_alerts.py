"""Tests for the alert send functions in src/alerts.py.

These mock the network layer (requests / twilio) so nothing is sent. They pin
the contract every sender shares: return True only on success, False on any
failure, and never raise.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.alerts import send_alert, send_whatsapp_alert, send_ntfy_alert


# ── ntfy ─────────────────────────────────────────────────────────────────────

def test_send_ntfy_posts_to_base_topic_with_auth():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        ok = send_ntfy_alert("http://127.0.0.1:2586", "emcure-alerts", "tk_secret", "hello")

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "http://127.0.0.1:2586/emcure-alerts"
    assert kwargs["data"] == "hello".encode("utf-8")
    assert kwargs["headers"]["Authorization"] == "Bearer tk_secret"


def test_send_ntfy_strips_trailing_slash_and_omits_auth_when_no_token():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        ok = send_ntfy_alert("http://127.0.0.1:2586/", "topic", "", "hi")

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "http://127.0.0.1:2586/topic"
    assert "Authorization" not in kwargs["headers"]


def test_send_ntfy_title_is_ascii_only():
    # A non-ASCII title would crash header encoding — it must be stripped.
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        send_ntfy_alert("http://h", "t", "", "body", title="EMCURE 🚀 alert")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Title"] == "EMCURE  alert"


def test_send_ntfy_non_200_returns_false():
    with patch("requests.post") as mock_post:
        mock_post.return_value.status_code = 403
        assert send_ntfy_alert("http://h", "t", "tok", "body") is False


def test_send_ntfy_exception_returns_false():
    with patch("requests.post", side_effect=ConnectionError("boom")):
        assert send_ntfy_alert("http://h", "t", "tok", "body") is False


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
