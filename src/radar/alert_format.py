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
    _SMA7_GAP_FRAC,
    ATR_BREAKOUT,
    GAP_REVERSION,
    RVOL_REVERSAL,
    SMA7_REVERSION,
    VWAP_PULLBACK,
    SignalHit,
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


def _regime_plain(regime: str) -> str:
    if regime == TRENDING_BULL:
        return "trending up"
    if regime == TRENDING_BEAR:
        return "trending down (risky)"
    return "choppy / sideways"


def format_opportunity(
    signal: SignalHit, confidence: int, regime: str, price: float
) -> str:
    """The single-signal 🚨 trade-idea message (for manual review only)."""
    lo, hi = signal.entry_zone
    conditions = "\n".join(f"• {c}" for c in signal.conditions)
    return (
        f"🚨 Trade idea — {signal.stock}\n\n"
        f"{signal_label(signal.signal_type)}  ·  now ₹{price:.2f}\n"
        f"Buy zone ₹{lo:.2f}–₹{hi:.2f}\n"
        f"Target ₹{signal.target:.2f}  ·  safety exit ₹{signal.stop:.2f}  "
        f"(risk/reward {signal.rr:.1f})\n"
        f"Confidence {confidence}/100  ·  market {_regime_plain(regime)}\n\n"
        f"Why:\n{conditions}\n\n"
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
    emoji = "🟢" if change_pct >= 0 else "🔴"
    gap_frac = snap.gap_to_sma7 / snap.price if snap.price else 0.0
    watch_zone = round(snap.sma7 * (1 - _WATCH_FRAC), 2)

    # One plain-English line on how the day looked (no RSI/MACD jargon).
    if snap.rsi >= 55 and snap.macd_hist > 0:
        mood = "Looking strong 📈"
    elif snap.rsi <= 35:
        mood = "Beaten down — could bounce back 🔄"
    elif snap.macd_hist > 0:
        mood = "Ticking upward"
    else:
        mood = "Soft / drifting"

    # What the radar is watching for tomorrow, in plain words.
    if gap_frac <= -_WATCH_FRAC:
        tomorrow = "🔔 In the radar's buy zone right now"
        watch = f"Radar would buy on a dip to about ₹{watch_zone:,.2f} or lower."
    elif gap_frac <= -_WATCH_FRAC / 2:
        tomorrow = "👀 Getting close to a buy"
        watch = f"Radar buys if it dips to ₹{watch_zone:,.2f} or lower."
    elif gap_frac > 0:
        tomorrow = "⏳ Above its average — no buy yet"
        watch = f"Radar buys only if it dips to ₹{watch_zone:,.2f} or lower."
    else:
        tomorrow = "📊 Near its average"
        watch = f"Radar buys if it dips to ₹{watch_zone:,.2f} or lower."

    return "\n".join([
        f"🌆 *{snap.stock} — end of day*  ·  {now.strftime('%a, %d %b')}",
        f"Closed ₹{close:,.2f}  {emoji} {change_pct:+.2f}%   (day ₹{snap.day_low:,.2f}–₹{snap.day_high:,.2f})",
        f"{mood}",
        "",
        f"Tomorrow: {tomorrow}",
        watch,
        "",
        _FOOTER,
    ])


def format_digest(
    items: list[tuple[SignalHit, int]], regime: str
) -> str:
    """Compact multi-signal digest for lower-ranked hits (one Telegram message)."""
    lines = [f"📡 A few more ideas to review — market {_regime_plain(regime)}", ""]
    for sig, conf in items:
        lines.append(
            f"• {sig.stock} — {signal_label(sig.signal_type)}  ·  "
            f"target ₹{sig.target:.1f}, exit ₹{sig.stop:.1f}  "
            f"(confidence {conf}, risk/reward {sig.rr:.1f})"
        )
    lines += ["", _FOOTER]
    return "\n".join(lines)
