"""Config loading: TOML sections, defaults, env overrides, derived helpers."""
from __future__ import annotations

from datetime import time as dtime

from src.kittybot.config import KittyBotConfig, load_config, parse_hhmm


def test_defaults_are_safe_paper_mode():
    cfg = KittyBotConfig()
    assert cfg.broker == "paper"
    assert cfg.live is False
    assert cfg.sends_real_orders is False


def test_parse_hhmm():
    assert parse_hhmm("09:30") == dtime(9, 30)
    assert parse_hhmm("15:10") == dtime(15, 10)


def test_derived_time_properties():
    cfg = KittyBotConfig()
    assert cfg.select_time_t == dtime(9, 30)
    assert cfg.hard_exit_t == dtime(15, 10)
    assert cfg.no_trade_after_t == dtime(10, 30)


def test_sends_real_orders_requires_live_and_non_paper():
    from dataclasses import replace
    base = KittyBotConfig()
    assert replace(base, live=True, broker="paper").sends_real_orders is False
    assert replace(base, live=False, broker="kite").sends_real_orders is False
    assert replace(base, live=True, broker="kite").sends_real_orders is True


def test_load_config_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg == KittyBotConfig()


def test_load_config_reads_toml_sections(tmp_path):
    path = tmp_path / "kittybot.toml"
    path.write_text(
        "[capital]\ncapital = 250000.0\nrisk_per_trade_pct = 0.5\n"
        "[filters]\ngap_max_pct = 2.0\n"
        "[timing]\nhard_exit = \"15:00\"\n"
        "[picks]\nfallback_universe = [\"AAA\", \"bbb\"]\n"
    )
    cfg = load_config(path)
    assert cfg.capital == 250000.0
    assert cfg.risk_per_trade_pct == 0.5
    assert cfg.gap_max_pct == 2.0
    assert cfg.hard_exit_t == dtime(15, 0)
    assert cfg.fallback_universe == ("AAA", "BBB")  # upcased tuple


def test_env_override_beats_toml(tmp_path, monkeypatch):
    path = tmp_path / "kittybot.toml"
    path.write_text("[capital]\ncapital = 100000.0\n[execution]\nbroker = \"paper\"\n")
    monkeypatch.setenv("KITTYBOT_CAPITAL", "500000")
    monkeypatch.setenv("KITTYBOT_BROKER", "kite")
    monkeypatch.setenv("KITTYBOT_LIVE", "true")
    cfg = load_config(path)
    assert cfg.capital == 500000.0
    assert cfg.broker == "kite"
    assert cfg.live is True
    assert cfg.sends_real_orders is True


def test_bad_env_value_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("KITTYBOT_CAPITAL", "not-a-number")
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg.capital == KittyBotConfig().capital


def test_load_config_without_toml_parser_degrades_to_defaults(tmp_path, monkeypatch):
    # Python 3.10 with no tomli installed: config file is ignored, not fatal.
    path = tmp_path / "kittybot.toml"
    path.write_text("[capital]\ncapital = 999999.0\n")
    monkeypatch.setattr("src.kittybot.config.tomllib", None)
    cfg = load_config(path)
    assert cfg.capital == KittyBotConfig().capital  # file skipped, default kept


def test_env_override_still_applies_without_toml_parser(tmp_path, monkeypatch):
    monkeypatch.setattr("src.kittybot.config.tomllib", None)
    monkeypatch.setenv("KITTYBOT_CAPITAL", "250000")
    cfg = load_config(tmp_path / "kittybot.toml")
    assert cfg.capital == 250000.0  # env path is independent of the TOML parser
