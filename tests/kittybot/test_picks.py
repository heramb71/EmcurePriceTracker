"""Kitty loading/parsing: JSON, clamping, dedupe, fallback universe."""
from __future__ import annotations

import json

from src.kittybot.picks import fallback_kitty, load_kitty, parse_kitty, parse_pick

from .conftest import make_config


def test_parse_pick_clamps_target_into_2_5():
    hot = parse_pick({"symbol": "X", "suggested_target_pct": 9.0}, ratio=2.0)
    cold = parse_pick({"symbol": "Y", "suggested_target_pct": 0.5}, ratio=2.0)
    assert hot.suggested_target_pct == 5.0
    assert cold.suggested_target_pct == 2.0


def test_parse_pick_derives_stop_from_ratio_when_missing():
    p = parse_pick({"symbol": "X", "suggested_target_pct": 4.0}, ratio=2.0)
    assert p.suggested_stop_pct == 2.0  # target / ratio


def test_parse_pick_keeps_explicit_stop():
    p = parse_pick({"symbol": "X", "suggested_target_pct": 4.0, "suggested_stop_pct": 1.0},
                   ratio=2.0)
    assert p.suggested_stop_pct == 1.0


def test_parse_pick_rejects_missing_symbol():
    assert parse_pick({"score": 5.0}, ratio=2.0) is None


def test_parse_kitty_dedupes_and_limits_to_max():
    cfg = make_config(max_picks=2)
    raw = {"picks": [
        {"symbol": "A"}, {"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"},
    ]}
    kitty = parse_kitty(raw, cfg)
    assert [p.symbol for p in kitty.picks] == ["A", "B"]
    assert kitty.source == "json"


def test_parse_kitty_reads_generated_at():
    kitty = parse_kitty({"generated_at": "2026-07-06T08:45:00", "picks": [{"symbol": "A"}]},
                        make_config())
    assert kitty.generated_at is not None
    assert kitty.generated_at.hour == 8


def test_load_kitty_falls_back_when_file_missing(tmp_path):
    cfg = make_config(picks_path=str(tmp_path / "nope.json"))
    kitty = load_kitty(cfg)
    assert kitty.source == "fallback"
    assert len(kitty.picks) == cfg.max_picks


def test_load_kitty_reads_json_file(tmp_path):
    path = tmp_path / "daily_picks.json"
    path.write_text(json.dumps({
        "generated_at": "2026-07-06T08:45:00",
        "picks": [{"symbol": "TATAMOTORS", "suggested_target_pct": 3.0}],
    }))
    cfg = make_config(picks_path=str(path))
    kitty = load_kitty(cfg)
    assert kitty.source == "json"
    assert kitty.picks[0].symbol == "TATAMOTORS"


def test_load_kitty_falls_back_on_empty_picks(tmp_path):
    path = tmp_path / "daily_picks.json"
    path.write_text(json.dumps({"picks": []}))
    cfg = make_config(picks_path=str(path))
    assert load_kitty(cfg).source == "fallback"


def test_fallback_kitty_uses_universe():
    cfg = make_config(max_picks=3, fallback_universe=("AAA", "BBB", "CCC", "DDD"))
    kitty = fallback_kitty(cfg)
    assert [p.symbol for p in kitty.picks] == ["AAA", "BBB", "CCC"]
    assert kitty.generated_at is None
