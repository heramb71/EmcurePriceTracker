"""Telegram alert formatting for the radar.

Produces the brief's 🚨 TRADE OPPORTUNITY block with the mandatory manual-review
footer. The footer is non-negotiable: the radar is informational only and never
executes trades.
"""
from __future__ import annotations

from src.radar.signals import (
    ATR_BREAKOUT,
    GAP_REVERSION,
    RVOL_REVERSAL,
    SMA7_REVERSION,
    SignalHit,
    VWAP_PULLBACK,
)

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
