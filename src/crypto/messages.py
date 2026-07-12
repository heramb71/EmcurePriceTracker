"""
WhatsApp message formatters for the crypto trend tracker.

Message types:
  format_morning_briefing    — 8 AM combined BTC + ETH snapshot
  format_evening_summary     — 8 PM full technical read
  format_signal_alert        — intraday RSI / signal trigger

Portfolio-aware formatters (holdings P&L, book-profit and dip-buy alerts)
live in src/crypto/portfolio_messages.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

_SIGNAL_EMOJI = {
    "Strong Buy":  "🟢🟢",
    "Buy":         "🟢",
    "Hold":        "🟡",
    "Sell":        "🔴",
    "Strong Sell": "🔴🔴",
}

_TREND_EMOJI = {
    "Strong Uptrend": "🚀",
    "Uptrend":        "📈",
    "Ranging":        "〰️",
    "Downtrend":      "📉",
}


def _pbar(value: float, total: float = 1.0, width: int = 12) -> str:
    """Filled progress bar scaled to value/total."""
    ratio = max(0.0, min(1.0, value / total if total else 0))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _price_line(quote: dict) -> str:
    """One-liner: ₹price  ($usd)  ▲/▼ change%"""
    inr = quote["price_inr"]
    usd = quote["price_usd"]
    pct = quote["change_pct"]
    arrow = "▲" if pct >= 0 else "▼"
    sign = "+" if pct >= 0 else ""
    return f"₹{inr:,.0f}  (${usd:,.0f})  {arrow} {sign}{pct:.1f}%"


def _rsi_label(rsi: float) -> str:
    if rsi < 35:
        return f"{rsi:.0f} 🔥 Oversold"
    if rsi > 68:
        return f"{rsi:.0f} ⚠️ Overbought"
    return f"{rsi:.0f}"


def _momentum_label(rsi: float) -> str:
    if rsi >= 70:
        return "run up fast — may pull back soon ⚠️"
    if rsi >= 55:
        return "strong and rising 📈"
    if rsi >= 45:
        return "no clear direction"
    if rsi >= 30:
        return "a bit weak, but could recover 📉"
    return "unusually low — possible bounce zone 🔥"


def _macd_label(macd_hist: float) -> str:
    if macd_hist > 0:
        return "turning up ✅"
    return "turning down ❌"


def _asset_block_short(name: str, sym: str, quote: dict, sig: dict) -> list[str]:
    """Compact layman block used in morning briefing."""
    pct      = quote["change_pct"]
    arrow    = "▲" if pct >= 0 else "▼"
    sign     = "+" if pct >= 0 else ""
    signal   = sig["signal"]
    se       = _SIGNAL_EMOJI.get(signal, "⚪")
    te       = _TREND_EMOJI.get(sig["trend"], "📊")

    action = (
        "Good time to consider buying 👍"   if signal in ("Strong Buy", "Buy") else
        "Good time to consider selling 👎"  if signal in ("Strong Sell", "Sell") else
        "No clear signal — hold or wait"
    )

    return [
        f"*{name} ({sym})*",
        f"Price: ₹{quote['price_inr']:,.0f}  (${quote['price_usd']:,.0f})  {arrow} {sign}{pct:.1f}% today",
        f"Last 7 days: {sig['change_7d_pct']:+.1f}%",
        f"Trend: {te} {sig['trend']}",
        f"Momentum: {_momentum_label(sig['rsi'])}",
        f"{se} *{signal}* — {action}",
    ]


def format_morning_briefing(
    btc_quote: dict,
    btc_sig: dict,
    eth_quote: dict,
    eth_sig: dict,
    now: Optional[datetime] = None,
    portfolio_block: Optional[str] = None,
) -> str:
    """8:00 AM combined BTC + ETH morning snapshot."""
    if now is None:
        now = datetime.now()

    usd_inr   = btc_quote.get("usd_inr", 84.0)
    avg_score = (btc_sig["score"] + eth_sig["score"]) / 2

    if avg_score >= 0.65:
        outlook = "🟢 *Both look bullish* — decent time to consider buying"
    elif avg_score >= 0.55:
        outlook = "🟡 *Mildly positive* — wait for stronger confirmation"
    elif avg_score <= 0.35:
        outlook = "🔴 *Both look weak* — better to stay on the sidelines"
    else:
        outlook = "⚪ *Mixed signals* — no strong move expected either way"

    lines = [
        "🌅 *Good Morning — Crypto Update*",
        f"📅 {now.strftime('%a, %d %b %Y')}",
        f"💱 1 USD = ₹{usd_inr:.2f} today",
        "",
    ]
    lines += _asset_block_short("Bitcoin", "BTC", btc_quote, btc_sig)
    lines += [""]
    lines += _asset_block_short("Ethereum", "ETH", eth_quote, eth_sig)
    lines += [
        "",
        f"── *Overall Outlook* ──",
        outlook,
    ]
    if portfolio_block:
        lines += ["", portfolio_block]
    lines += [
        "",
        "⏰ Next update: 8:00 PM tonight",
    ]

    return "\n".join(lines)


def format_evening_summary(
    btc_quote: dict,
    btc_sig: dict,
    eth_quote: dict,
    eth_sig: dict,
    now: Optional[datetime] = None,
    portfolio_block: Optional[str] = None,
) -> str:
    """8:00 PM full technical evening summary."""
    if now is None:
        now = datetime.now()

    usd_inr = btc_quote.get("usd_inr", 84.0)

    lines = [
        "🌙 *Good Evening — Crypto Summary*",
        f"📅 {now.strftime('%a, %d %b %Y')}",
        f"💱 1 USD = ₹{usd_inr:.2f}",
        "",
    ]

    for name, sym, quote, sig in [
        ("Bitcoin",  "BTC", btc_quote, btc_sig),
        ("Ethereum", "ETH", eth_quote, eth_sig),
    ]:
        signal_emoji = _SIGNAL_EMOJI.get(sig["signal"], "⚪")
        pct   = quote["change_pct"]
        arrow = "▲" if pct >= 0 else "▼"
        sign  = "+" if pct >= 0 else ""

        support  = sig["bb_lower"] * usd_inr
        resist   = sig["bb_upper"] * usd_inr
        ema200   = sig["ema200"]   * usd_inr
        above_200 = quote["price_inr"] > ema200

        lines += [
            f"── *{name} ({sym})* ──",
            f"Price:    ₹{quote['price_inr']:,.0f}  (${quote['price_usd']:,.0f})",
            f"Today:    {arrow} {sign}{pct:.1f}%   |   Last 7 days: {sig['change_7d_pct']:+.1f}%",
            "",
            f"How it looks:",
            f"Momentum: {_momentum_label(sig['rsi'])}",
            f"Short-term trend: {_macd_label(sig['macd_hist'])}",
            f"Longer-term: {'above ✅' if above_200 else 'below ❌'} its 200-day average",
            "",
            f"Tends to bounce near ₹{support:,.0f}, struggle near ₹{resist:,.0f}",
            "",
            f"{signal_emoji} *{sig['signal']}*",
            "",
        ]

    if portfolio_block:
        lines += [portfolio_block, ""]
    lines += ["⏰ Next update: tomorrow 8:00 AM"]
    return "\n".join(lines)


def format_signal_alert(
    name: str,
    sym: str,
    quote: dict,
    sig: dict,
    now: Optional[datetime] = None,
) -> str:
    """Intraday alert when RSI crosses a threshold or signal becomes Strong Buy/Sell."""
    if now is None:
        now = datetime.now()

    usd_inr      = quote.get("usd_inr", 84.0)
    rsi          = sig["rsi"]
    signal       = sig["signal"]
    signal_emoji = _SIGNAL_EMOJI.get(signal, "⚪")
    pct          = quote["change_pct"]
    arrow        = "▲" if pct >= 0 else "▼"
    sign         = "+" if pct >= 0 else ""

    if rsi < 35:
        rsi_note = "🔥 Price is unusually low compared to recent history — historically a good accumulation zone."
    elif rsi > 68:
        rsi_note = "⚠️ Price has run up fast and may be due for a pullback. Don't chase."
    else:
        rsi_note = ""

    action = (
        "This could be a good buying opportunity."  if signal in ("Strong Buy", "Buy") else
        "Consider reducing or exiting your position." if signal in ("Strong Sell", "Sell") else
        "No strong action needed right now."
    )

    lines = [
        f"{signal_emoji} *{name} Alert — {signal}*",
        f"⏰ {now.strftime('%d %b %Y, %H:%M IST')}",
        "",
        f"Price: ₹{quote['price_inr']:,.0f}  (${quote['price_usd']:,.0f})",
        f"Today: {arrow} {sign}{pct:.1f}%   |   Last 7 days: {sig['change_7d_pct']:+.1f}%",
        "",
        f"How it looks:",
        f"Momentum: {_momentum_label(rsi)}",
        f"Short-term trend: {_macd_label(sig['macd_hist'])}",
        "",
        f"Tends to bounce near ₹{sig['bb_lower'] * usd_inr:,.0f}, "
        f"struggle near ₹{sig['bb_upper'] * usd_inr:,.0f}",
    ]

    if rsi_note:
        lines += ["", rsi_note]

    lines += ["", f"👉 {action}"]

    return "\n".join(lines)
