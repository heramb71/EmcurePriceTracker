"""KittyBot Telegram alerts: pure formatters + engine wiring via a fake notifier."""
from __future__ import annotations

import json
from datetime import datetime

from src.kittybot import marketdata, notify
from src.kittybot.engine import KittyBotEngine
from src.kittybot.filters import OpenQuote
from src.kittybot.opening_range import LONG, OpeningRange
from src.kittybot.risk import plan_trade

from .conftest import make_config, make_pick

TODAY = datetime(2026, 7, 6)


# ── pure formatters ───────────────────────────────────────────────────────────
def test_daily_plan_lists_survivors():
    picks = [make_pick("TATAMOTORS"), make_pick("VEDL", long_room_2pct=1.0, short_room_2pct=3.0)]
    msg = notify.format_daily_plan(picks, source="json", live=False)
    assert "TATAMOTORS" in msg and "VEDL" in msg
    assert "SHORT-ok" in msg  # VEDL has more short room
    assert "paper" in msg


def test_daily_plan_empty():
    msg = notify.format_daily_plan([], source="fallback", live=False)
    assert "no candidates" in msg.lower()


def test_entry_message_has_levels_and_live_footer():
    plan = plan_trade("TATAMOTORS", LONG, 100.0, 3.0, 1.0, 100_000, 1.0)
    msg = notify.format_entry(plan, fill_price=100.1, live=True)
    assert "LONG" in msg
    assert "Target" in msg and "Stop" in msg
    assert "LIVE" in msg  # live footer, not paper


def test_exit_message_reflects_pnl_sign():
    win = notify.format_exit("X", "TARGET", 103.0, 3000.0, live=False)
    loss = notify.format_exit("X", "STOP", 99.0, -1000.0, live=False)
    assert "✅" in win and "+3,000" in win.replace(" ", "")
    assert "🔻" in loss and "-1,000" in loss.replace(" ", "")


def test_skip_message_lists_reasons():
    msg = notify.format_skip(["India VIX spike: 14 vs 11"], live=False)
    assert "no trade today" in msg.lower()
    assert "VIX" in msg


def test_breakeven_message():
    assert "breakeven" in notify.format_breakeven("X", 100.0, live=False).lower()


# ── notifier resolves channel; disabled when unconfigured ────────────────────
def test_notifier_disabled_without_channel(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_RADAR_TOKEN", raising=False)
    n = notify.KittyNotifier()  # defaults to the radar feed
    assert n.enabled is False
    # No exception when sending to a disabled notifier.
    n.entry(plan_trade("X", LONG, 100.0, 3.0, 1.0, 100_000, 1.0), 100.0)


def test_notifier_reuses_radar_channel(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_alert",
                        lambda tok, chat, msg: sent.append((tok, chat, msg)) or True)
    monkeypatch.setenv("TELEGRAM_RADAR_TOKEN", "rt")
    monkeypatch.setenv("TELEGRAM_RADAR_CHAT_ID", "rc")
    n = notify.KittyNotifier()  # radar feed by default
    assert n.enabled is True
    n.skip(["stale picks"])
    assert sent and sent[0][0] == "rt" and "stale picks" in sent[0][2]


# ── engine fires alerts through the notifier ─────────────────────────────────
class SpyNotifier:
    def __init__(self):
        self.calls = []

    def daily_plan(self, survivors, source): self.calls.append(("daily_plan", len(survivors)))
    def skip(self, reasons): self.calls.append(("skip", tuple(reasons)))
    def entry(self, plan, fill): self.calls.append(("entry", plan.symbol))
    def breakeven(self, symbol, stop): self.calls.append(("breakeven", symbol))
    def exit(self, symbol, reason, price, pnl): self.calls.append(("exit", symbol, reason))

    def kinds(self):
        return [c[0] for c in self.calls]


def _wire(tmp_path, monkeypatch):
    picks_path = tmp_path / "daily_picks.json"
    picks_path.write_text(json.dumps({
        "generated_at": TODAY.replace(hour=8, minute=45).isoformat(),
        "picks": [{"symbol": "TATAMOTORS", "atr14_pct": 2.0, "long_room_2pct": 3.0,
                   "short_room_2pct": 1.0, "suggested_target_pct": 3.0,
                   "suggested_stop_pct": 1.5, "prev_close": 100.0}],
    }))
    cfg = make_config(picks_path=str(picks_path), state_path=str(tmp_path / "s.json"),
                      journal_dir=str(tmp_path / "j"))
    monkeypatch.setattr(marketdata, "opening_quote",
                        lambda s: OpenQuote(open=100.0, prev_close=100.0))
    monkeypatch.setattr(marketdata, "india_vix", lambda: (11.0, 11.0))
    monkeypatch.setattr(marketdata, "opening_range",
                        lambda s, m: OpeningRange(105.0, 95.0, 15000.0, 1000.0))
    monkeypatch.setattr(marketdata, "live_tick", lambda s: (106.0, 1500.0))

    class Fake:
        name = "fake"
        def place_market(self, sym, qty, side, product):
            from src.kittybot.broker import Fill
            return Fill(order_id="F1", side=side, qty=qty, price=106.0, status="COMPLETE")

    spy = SpyNotifier()
    return cfg, KittyBotEngine(cfg, broker=Fake(), notifier=spy), spy


def test_engine_alerts_on_plan_and_entry_and_exit(tmp_path, monkeypatch):
    cfg, engine, spy = _wire(tmp_path, monkeypatch)
    engine.step(TODAY.replace(hour=9, minute=15))   # prepare → daily_plan
    engine.step(TODAY.replace(hour=9, minute=31))   # breakout → entry
    monkeypatch.setattr(marketdata, "live_tick", lambda s: (110.0, 1500.0))
    engine.step(TODAY.replace(hour=12, minute=0))   # breakeven + target exit
    kinds = spy.kinds()
    assert "daily_plan" in kinds
    assert "entry" in kinds
    assert "breakeven" in kinds
    assert ("exit", "TATAMOTORS", "TARGET") in spy.calls
