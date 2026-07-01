"""Unit tests for src/channels.py — alert-channel resolution."""
from __future__ import annotations

import pytest

from src import channels

# Every env var the module reads, cleared before each test for isolation.
_ENV_KEYS = [
    "WHATSAPP_ENABLED",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_EMCURE_TOKEN",
    "TELEGRAM_EMCURE_CHAT_ID",
    "TELEGRAM_RADAR_TOKEN",
    "TELEGRAM_RADAR_CHAT_ID",
    "TELEGRAM_CRYPTO_TOKEN",
    "TELEGRAM_CRYPTO_CHAT_ID",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestWhatsappEnabled:
    def test_defaults_off(self):
        assert channels.whatsapp_enabled() is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", " true "])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("WHATSAPP_ENABLED", value)
        assert channels.whatsapp_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "", "off"])
    def test_non_true_values_disable(self, monkeypatch, value):
        monkeypatch.setenv("WHATSAPP_ENABLED", value)
        assert channels.whatsapp_enabled() is False


class TestTelegramConfig:
    def test_returns_empty_when_nothing_configured(self):
        assert channels.telegram_config("emcure") == ("", "")

    def test_falls_back_to_shared(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "shared-tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "shared-chat")
        for service in channels.SERVICES:
            assert channels.telegram_config(service) == ("shared-tok", "shared-chat")

    def test_per_service_overrides_shared(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_TOKEN", "shared-tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "shared-chat")
        monkeypatch.setenv("TELEGRAM_RADAR_TOKEN", "radar-tok")
        monkeypatch.setenv("TELEGRAM_RADAR_CHAT_ID", "radar-chat")

        assert channels.telegram_config("radar") == ("radar-tok", "radar-chat")
        # Other services still resolve to the shared bot.
        assert channels.telegram_config("emcure") == ("shared-tok", "shared-chat")
        assert channels.telegram_config("crypto") == ("shared-tok", "shared-chat")

    def test_feeds_are_isolated(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_EMCURE_TOKEN", "e-tok")
        monkeypatch.setenv("TELEGRAM_EMCURE_CHAT_ID", "e-chat")
        monkeypatch.setenv("TELEGRAM_CRYPTO_TOKEN", "c-tok")
        monkeypatch.setenv("TELEGRAM_CRYPTO_CHAT_ID", "c-chat")

        assert channels.telegram_config("emcure") == ("e-tok", "e-chat")
        assert channels.telegram_config("crypto") == ("c-tok", "c-chat")
        # No shared fallback set and radar has no override → empty.
        assert channels.telegram_config("radar") == ("", "")

    def test_partial_override_mixes_with_shared(self, monkeypatch):
        # Service overrides the token but not the chat id → chat id falls back.
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "shared-chat")
        monkeypatch.setenv("TELEGRAM_CRYPTO_TOKEN", "crypto-tok")
        assert channels.telegram_config("crypto") == ("crypto-tok", "shared-chat")

    def test_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_EMCURE_TOKEN", "  tok  ")
        monkeypatch.setenv("TELEGRAM_EMCURE_CHAT_ID", "  chat  ")
        assert channels.telegram_config("emcure") == ("tok", "chat")

    def test_unknown_service_raises(self):
        with pytest.raises(ValueError):
            channels.telegram_config("stocks")
