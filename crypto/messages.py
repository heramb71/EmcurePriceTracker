"""
WhatsApp message formatters for the crypto trend tracker.

Three message types:
  format_morning_briefing  — 8 AM combined BTC + ETH snapshot
  format_evening_summary   — 8 PM full technical read
  format_signal_alert      — intraday RSI / signal trigger
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


def _asset_block_short(name: str, sym: str, quote: dict, sig: dict) -> list[str]:
    """Compact block used in morning briefing."""
    usd_inr = quote["usd_inr"]
    return [
        f"*{name} ({sym})*",
        _price_line(quote),
        f"{_SIGNAL_EMOJI.get(sig['signal'], '⚪')} {sig['signal']}  ·  "
        f"{_TREND_EMOJI.get(sig['trend'], '📊')} {sig['trend']}",
        f"RSI {_rsi_label(sig['rsi'])}  ·  7d {sig['change_7d_pct']:+.1f}%",
        f"BB  ₹{sig['bb_lower'] * usd_inr:,.0f} – ₹{sig['bb_upper'] * usd_inr:,.0f}",
    ]


def format_morning_briefing(
    btc_quote: dict,
    btc_sig: dict,
    eth_quote: dict,
    eth_sig: dict,
    now: Optional[datetime] = None,
) -> str:
    """8:00 AM combined BTC + ETH morning snapshot."""
    if now is None:
        now = datetime.now()

    usd_inr = btc_quote.get("usd_inr", 84.0)
    avg_score = (btc_sig["score"] + eth_sig["score"]) / 2

    if avg_score >= 0.65:
        outlook = "🟢 *Bullish* — both assets in buy territory"
    elif avg_score >= 0.55:
        outlook = "🟡 *Mildly Bullish* — watch for confirmation"
    elif avg_score <= 0.35:
        outlook = "🔴 *Bearish* — caution warranted"
    else:
        outlook = "⚪ *Neutral* — no strong directional edge"

    lines = [
        "📊 *Crypto Morning Briefing*",
        f"📅 {now.strftime('%a %d %b %Y  %H:%M IST')}",
        f"💱 USD/INR ₹{usd_inr:.2f}",
        "",
        "──────────────────────────",
    ]
    lines += _asset_block_short("Bitcoin", "BTC", btc_quote, btc_sig)
    lines += ["", "──────────────────────────"]
    lines += _asset_block_short("Ethereum", "ETH", eth_quote, eth_sig)
    lines += [
        "",
        "──────────────────────────",
        "*Overall Outlook*",
        outlook,
        "",
        f"BTC score {btc_sig['score']:.2f}  ·  ETH score {eth_sig['score']:.2f}",
        "",
        "⏰ Next update: 8:00 PM IST",
    ]

    return "\n".join(lines)


def format_evening_summary(
    btc_quote: dict,
    btc_sig: dict,
    eth_quote: dict,
    eth_sig: dict,
    now: Optional[datetime] = None,
) -> str:
    """8:00 PM full technical evening summary."""
    if now is None:
        now = datetime.now()

    usd_inr = btc_quote.get("usd_inr", 84.0)

    lines = [
        "🌙 *Crypto Evening Summary*",
        f"📅 {now.strftime('%a %d %b %Y  %H:%M IST')}",
        f"💱 USD/INR ₹{usd_inr:.2f}",
        "",
    ]

    for name, sym, quote, sig in [
        ("Bitcoin",  "BTC", btc_quote, btc_sig),
        ("Ethereum", "ETH", eth_quote, eth_sig),
    ]:
        signal_emoji = _SIGNAL_EMOJI.get(sig["signal"], "⚪")
        trend_emoji = _TREND_EMOJI.get(sig["trend"], "📊")
        pct = quote["change_pct"]
        arrow = "▲" if pct >= 0 else "▼"
        sign = "+" if pct >= 0 else ""

        lines += [
            f"──────────────────────────",
            f"*{name} ({sym})*",
            "```",
            f"Price   ₹{quote['price_inr']:>12,.0f}  (${quote['price_usd']:,.0f})",
            f"24h     {arrow} {sign}{pct:.1f}%",
            f"7d      {sig['change_7d_pct']:+.1f}%",
            f"RSI     {sig['rsi']:.0f}",
            f"MACD h  {sig['macd_hist']:+.4f}",
            f"EMA20   ₹{sig['ema20'] * usd_inr:,.0f}",
            f"EMA50   ₹{sig['ema50'] * usd_inr:,.0f}",
            f"EMA200  ₹{sig['ema200'] * usd_inr:,.0f}",
            f"BB Lo   ₹{sig['bb_lower'] * usd_inr:,.0f}",
            f"BB Hi   ₹{sig['bb_upper'] * usd_inr:,.0f}",
            f"ATR     ₹{sig['atr'] * usd_inr:,.0f}  ({sig['atr_pct']:.1f}%)",
            "```",
            f"{signal_emoji} {sig['signal']}  ·  {trend_emoji} {sig['trend']}",
            "",
        ]

    lines += [
        "──────────────────────────",
        "⏰ Next update: tomorrow 8:00 AM IST",
    ]

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

    usd_inr = quote.get("usd_inr", 84.0)
    rsi = sig["rsi"]
    signal = sig["signal"]
    signal_emoji = _SIGNAL_EMOJI.get(signal, "⚪")

    rsi_note = ""
    if rsi < 35:
        rsi_note = "🔥 RSI oversold — historically a strong accumulation zone"
    elif rsi > 68:
        rsi_note = "⚠️ RSI overbought — elevated risk, consider waiting for pullback"

    lines = [
        f"{signal_emoji} *{name} ({sym}) — {signal}*",
        f"📅 {now.strftime('%d %b %Y  %H:%M IST')}",
        "",
        _price_line(quote),
        f"7d change  {sig['change_7d_pct']:+.1f}%",
        "",
        f"RSI     {_rsi_label(rsi)}",
        f"MACD h  {sig['macd_hist']:+.4f}",
        f"Trend   {_TREND_EMOJI.get(sig['trend'], '📊')} {sig['trend']}",
        f"Score   {sig['score']:.2f}",
        "",
        f"BB Lo   ₹{sig['bb_lower'] * usd_inr:,.0f}",
        f"BB Hi   ₹{sig['bb_upper'] * usd_inr:,.0f}",
        f"EMA50   ₹{sig['ema50'] * usd_inr:,.0f}",
    ]

    if rsi_note:
        lines += ["", rsi_note]

    return "\n".join(lines)
