"""Telegram alert formatting for the radar.

Produces the brief's 🚨 TRADE OPPORTUNITY block with the mandatory manual-review
footer. The footer is non-negotiable: the radar is informational only and never
executes trades.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from src.radar.features import StockFeatures
from src.radar.regime import TRENDING_BEAR, TRENDING_BULL
from src.radar.signals import (
    ATR_BREAKOUT,
    GAP_REVERSION,
    RVOL_REVERSAL,
    SMA7_REVERSION,
    _SMA7_GAP_FRAC,
    SignalHit,
    VWAP_PULLBACK,
)

_IST = timezone(timedelta(hours=5, minutes=30))

# Reversion watch zones as a *fraction* below the 7-day average so they scale
# across price levels (₹70 SUZLON ↔ ₹1800 EMCURE). _WATCH_FRAC is locked to the
# live SMA7 signal's own threshold; _STRONG_FRAC is a slightly deeper dip.
_WATCH_FRAC = _SMA7_GAP_FRAC   # 1.4% below SMA7 — the signal's trigger
_STRONG_FRAC = 0.018           # ~1.8% below (EMCURE ₹25/₹1400 ≈ 1.8%)

_SIGNAL_LABELS = {
    SMA7_REVERSION: "SMA7 Mean Reversion",
    VWAP_PULLBACK: "VWAP Pullback",
    RVOL_REVERSAL: "High Relative Volume Reversal",
    ATR_BREAKOUT: "ATR Expansion Breakout",
    GAP_REVERSION: "Gap Reversion Opportunity",
}

_FOOTER = (
    "Manual Review Required.\n"
    "This is an informational signal only.\n"
    "No automatic execution."
)


def signal_label(signal_type: str) -> str:
    return _SIGNAL_LABELS.get(signal_type, signal_type)


def format_opportunity(
    signal: SignalHit, confidence: int, regime: str, price: float
) -> str:
    """The single-signal 🚨 TRADE OPPORTUNITY message."""
    lo, hi = signal.entry_zone
    conditions = "\n".join(f"✓ {c}" for c in signal.conditions)
    return (
        "🚨 TRADE OPPORTUNITY\n\n"
        f"Stock: {signal.stock}\n"
        f"Signal: {signal_label(signal.signal_type)}\n"
        f"Current Price: ₹{price:.2f}\n"
        f"Confidence Score: {confidence}/100\n"
        f"Market Regime: {regime}\n\n"
        "Reason:\n"
        f"{conditions}\n\n"
        f"Suggested Entry Zone: ₹{lo:.2f} – ₹{hi:.2f}\n"
        f"Suggested Stop Loss: ₹{signal.stop:.2f}\n"
        f"Suggested Target: ₹{signal.target:.2f}\n"
        f"Risk:Reward: {signal.rr:.2f}\n\n"
        f"{_FOOTER}"
    )


def _rsi_line(rsi: float) -> str:
    if rsi >= 70:
        return f"📈 Momentum (RSI {rsi:.0f}): Overbought — may pull back soon"
    if rsi >= 55:
        return f"📈 Momentum (RSI {rsi:.0f}): Strong upward momentum"
    if rsi >= 45:
        return f"➡️ Momentum (RSI {rsi:.0f}): Neutral — no clear direction"
    if rsi >= 30:
        return f"📉 Momentum (RSI {rsi:.0f}): Weak, but recovery possible"
    return f"📉 Momentum (RSI {rsi:.0f}): Oversold — possible bounce zone"


def _macd_line(macd_hist: float) -> str:
    if macd_hist > 0:
        return "✅ Trend (MACD): Short-term trend is turning UP"
    return "❌ Trend (MACD): Short-term trend is turning DOWN"


def _regime_line(regime: str) -> str:
    if regime == TRENDING_BULL:
        return "🟢 Market regime: Trending upward — supportive for setups"
    if regime == TRENDING_BEAR:
        return "🔴 Market regime: Trending downward — high risk"
    return "🟡 Market regime: Choppy / sideways — trade carefully"


def format_eod_stock(
    snap: StockFeatures, regime: str, now: Optional[datetime] = None
) -> str:
    """End-of-day per-stock summary in the live engine's house style.

    Watch-only: unlike the EMCURE engine this carries no probability/confidence
    claim for tomorrow (the reversion edge is not validated outside EMCURE), only
    the SMA7 reversion zones the radar is watching, plus the manual-review footer.
    """
    if now is None:
        now = datetime.now(_IST)

    close = snap.price
    change_pct = (
        (close - snap.prev_close) / snap.prev_close * 100 if snap.prev_close else 0.0
    )
    change_emoji = "🟢" if change_pct >= 0 else "🔴"

    gap = snap.gap_to_sma7  # price - sma7 (negative = below the average)
    gap_frac = gap / snap.price if snap.price else 0.0
    watch_zone = round(snap.sma7 * (1 - _WATCH_FRAC), 2)
    strong_zone = round(snap.sma7 * (1 - _STRONG_FRAC), 2)
    setup_signal = (
        "🔔 SETUP FORMING" if gap_frac <= -_WATCH_FRAC else
        "👀 WATCH ZONE"    if gap_frac <= -_WATCH_FRAC / 2 else
        "⏳ TOO FAR — above the average, wait" if gap_frac > 0 else
        "📊 Near the 7-day average"
    )

    lines = [
        f"🌆 {snap.stock} — End of Day Summary",
        f"📅 {now.strftime('%a, %d %b %Y')}",
        "",
        f"Opened:  ₹{snap.open:,.2f}",
        f"Highest: ₹{snap.day_high:,.2f}",
        f"Lowest:  ₹{snap.day_low:,.2f}",
        f"Closed:  ₹{close:,.2f}  {change_emoji} {change_pct:+.2f}%",
        "",
        "📊 Today's Market Conditions:",
        _rsi_line(snap.rsi),
        _macd_line(snap.macd_hist),
        _regime_line(regime),
        "",
        "── Tomorrow's Outlook ──",
        setup_signal,
    ]

    if gap_frac <= -_WATCH_FRAC / 2:
        lines += [
            f"Trading {abs(gap_frac) * 100:.1f}% below its 7-day average — reversion zone.",
            f"Radar watch zone: ₹{watch_zone:,.2f} or below",
            f"Strong zone: ₹{strong_zone:,.2f} or below",
        ]
    else:
        lines.append(
            f"7-day average ₹{snap.sma7:,.2f} — radar watches ₹{watch_zone:,.2f} or below "
            f"({_WATCH_FRAC * 100:.1f}% dip)."
        )

    lines += ["", _FOOTER]
    return "\n".join(lines)


def format_digest(
    items: list[tuple[SignalHit, int]], regime: str
) -> str:
    """Compact multi-signal digest for lower-ranked hits (one Telegram message)."""
    lines = [f"📡 RADAR DIGEST — {regime}", ""]
    for sig, conf in items:
        lines.append(
            f"• {sig.stock} — {signal_label(sig.signal_type)} "
            f"({conf}/100) | T ₹{sig.target:.1f} / SL ₹{sig.stop:.1f} "
            f"| RR {sig.rr:.1f}"
        )
    lines += ["", _FOOTER]
    return "\n".join(lines)
