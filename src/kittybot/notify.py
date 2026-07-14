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

def _pct_with_rupees(pct: float, prev_close: Optional[float]) -> str:
    """``2.4% ≈ ₹34`` — the rupee move off yesterday's close, when known."""
    if not prev_close:
        return f"{pct:.1f}%"
    return f"{pct:.1f}% ≈ ₹{prev_close * pct / 100:,.0f}"


def format_daily_plan(survivors: list[Pick], source: str, live: bool) -> str:
    """Pre-open briefing: today's kitty after earnings/gap discards."""
    if not survivors:
        return "\n".join(["🐾 *KittyBot — no candidates today*",
                          "", "All picks were discarded (earnings / gap / no quote).",
                          "", _foot(live)])
    lines = [f"🐾 *KittyBot — today's watch ({len(survivors)})*",
             f"_source: {source}_", ""]
    any_both, any_long_only = False, False
    for p in survivors:
        # Mirrors the engine's gate: shorts allowed only when short room ≥ long room.
        both_ways = p.short_room_2pct >= p.long_room_2pct
        side = "↕️ long or short" if both_ways else "⬆️ long only"
        any_both, any_long_only = any_both or both_ways, any_long_only or not both_ways
        px = f"  ₹{p.prev_close:,.0f}" if p.prev_close else ""
        lines.append(f"• *{p.symbol}*{px}")
        lines.append(f"   tgt {_pct_with_rupees(p.suggested_target_pct, p.prev_close)} · "
                     f"sl {_pct_with_rupees(p.suggested_stop_pct, p.prev_close)} · {side}")
    lines.append("")
    if any_long_only:
        lines.append("⬆️ long only — moves up more easily; buys a break *above* the opening range")
    if any_both:
        lines.append("↕️ long or short — falls as easily as it rises; a break *below* the range may be shorted")
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
