"""Telegram alerts for KittyBot — the actionable feed that replaces radar's alerts.

The radar scanner pushed "trade opportunity — review manually" messages to its
Telegram bot. KittyBot supersedes it, so it pushes the *decisions it actually
took*: the morning kitty, the single entry, the breakeven move, and the exit with
P&L — plus the skips, so a quiet day is explained rather than silent.

Message formatters are pure (return strings) and unit-tested; :class:`KittyNotifier`
is the thin I/O shell that resolves the ``radar`` Telegram channel (the feed the
retired scanner used — so alerts land on the bot you already watch, falling back to
the shared bot) and sends. Telegram-only, matching radar. A missing channel degrades
to a no-op — alerts never block trading, they mirror what the journal already recorded.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.kittybot.picks import Pick
from src.kittybot.risk import TradePlan
from src.notify import channels
from src.notify.alerts import send_alert

logger = logging.getLogger("kittybot.notify")

_FOOTER = "🐾 KittyBot · paper"
_FOOTER_LIVE = "🐾 KittyBot · LIVE"


def _foot(live: bool) -> str:
    return _FOOTER_LIVE if live else _FOOTER


# ── pure formatters ───────────────────────────────────────────────────────────

def format_daily_plan(survivors: list[Pick], source: str, live: bool) -> str:
    """Pre-open briefing: today's kitty after earnings/gap discards."""
    if not survivors:
        return "\n".join(["🐾 *KittyBot — no candidates today*",
                          "", "All picks were discarded (earnings / gap / no quote).",
                          "", _foot(live)])
    lines = [f"🐾 *KittyBot — today's watch ({len(survivors)})*",
             f"_source: {source}_", ""]
    for p in survivors:
        room = "LONG-bias" if p.long_room_2pct >= p.short_room_2pct else "SHORT-ok"
        lines.append(
            f"• *{p.symbol}*  tgt {p.suggested_target_pct:.1f}% / "
            f"sl {p.suggested_stop_pct:.1f}%  ({room})"
        )
    lines += ["", "Waiting for the 15-min opening-range break — one trade only.", "",
              _foot(live)]
    return "\n".join(lines)


def format_skip(reasons: list[str], live: bool) -> str:
    body = "\n".join(f"• {r}" for r in reasons) or "• (unspecified)"
    return "\n".join(["🚫 *KittyBot — no trade today*", "", body, "", _foot(live)])


def format_entry(plan: TradePlan, fill_price: float, live: bool) -> str:
    arrow = "🟢 LONG" if plan.direction == "LONG" else "🔴 SHORT"
    return "\n".join([
        f"{arrow} *KittyBot entry — {plan.symbol}*", "",
        f"Entry ₹{fill_price:,.2f}  ×  {plan.qty} sh",
        f"🎯 Target ₹{plan.target:,.2f}   🛑 Stop ₹{plan.stop:,.2f}",
        f"Risk ₹{plan.risk_rupees:,.0f}  ·  hard exit 15:10 IST", "",
        _foot(live),
    ])


def format_breakeven(symbol: str, stop: float, live: bool) -> str:
    return "\n".join([
        f"🛡️ *KittyBot — stop to breakeven ({symbol})*", "",
        f"+1% reached — stop moved to ₹{stop:,.2f}. Risk-free from here.", "",
        _foot(live),
    ])


def format_exit(symbol: str, reason: str, exit_price: float, pnl: float, live: bool) -> str:
    label = {"TARGET": "🎯 Target hit", "STOP": "🛑 Stopped out",
             "TIME": "⏰ Time exit (15:10)"}.get(reason, reason)
    emoji = "✅" if pnl >= 0 else "🔻"
    return "\n".join([
        f"{emoji} *KittyBot exit — {symbol}*", "",
        f"{label} @ ₹{exit_price:,.2f}",
        f"P&L: ₹{pnl:+,.0f}", "",
        _foot(live),
    ])


# ── I/O shell ─────────────────────────────────────────────────────────────────

class KittyNotifier:
    """Resolves the kitty Telegram channel once and sends formatted alerts."""

    def __init__(self, service: str = "radar", *, live: bool = False):
        self.live = live
        try:
            self.token, self.chat = channels.telegram_config(service)
        except ValueError:
            self.token, self.chat = "", ""
        self.enabled = bool(self.token and self.chat)
        if not self.enabled:
            logger.info("KittyNotifier: Telegram not configured for %r — alerts off "
                        "(decisions still journalled).", service)

    def _send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            return send_alert(self.token, self.chat, message)
        except Exception:
            logger.exception("KittyNotifier send failed")
            return False

    def daily_plan(self, survivors: list[Pick], source: str) -> None:
        self._send(format_daily_plan(survivors, source, self.live))

    def skip(self, reasons: list[str]) -> None:
        self._send(format_skip(reasons, self.live))

    def entry(self, plan: TradePlan, fill_price: float) -> None:
        self._send(format_entry(plan, fill_price, self.live))

    def breakeven(self, symbol: str, stop: float) -> None:
        self._send(format_breakeven(symbol, stop, self.live))

    def exit(self, symbol: str, reason: str, exit_price: float, pnl: float) -> None:
        self._send(format_exit(symbol, reason, exit_price, pnl, self.live))


def make_notifier(cfg) -> Optional[KittyNotifier]:
    """Build a notifier from config (``telegram_service`` + live flag)."""
    return KittyNotifier(cfg.telegram_service, live=cfg.sends_real_orders)
