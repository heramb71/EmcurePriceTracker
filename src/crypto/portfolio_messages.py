"""
Portfolio-aware message formatters (crypto_portfolio.json).

Split out of messages.py to keep both files inside the 400-line cap:
  format_portfolio_block     — holdings P&L + buy/sell targets (in briefings)
  format_book_profit_alert   — held coin entered the book-profit band
  format_dip_buy_alert       — held coin dipped into the SMA7 accumulation zone

See src/crypto/portfolio.py for the underlying math.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.crypto.portfolio import (
    STABLECOINS,
    dip_zone_inr,
    sell_targets_inr,
    tranche_inr,
)


def _inr(x: float) -> str:
    """₹ with paise for small-price coins (DOGE), whole rupees otherwise."""
    return f"₹{x:,.2f}" if abs(x) < 100 else f"₹{x:,.0f}"


def _signed_inr(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.0f}"


def format_portfolio_block(
    portfolio: dict,
    summary: dict,
    sigs: dict[str, dict],
    usd_inr: float,
) -> str:
    """Holdings P&L + book-profit targets + dip-buy zones, for the briefings.

    ``sigs`` maps symbol → signal dict (only BTC/ETH have one — dip zones are
    shown just for those). Stablecoins get the P&L line only.
    """
    plan = portfolio["plan"]
    lines = ["── *Your Portfolio* ──"]
    for c in summary["coins"]:
        sign = "+" if c["pnl_pct"] >= 0 else ""
        lines.append(
            f"{c['symbol']}: ₹{c['invested_inr']:,.0f} → ₹{c['value_inr']:,.0f}"
            f"  ({sign}{c['pnl_pct']:.1f}%)"
        )
    for sym in summary["missing"]:
        lines.append(f"{sym}: no live price — not counted")
    tsign = "+" if summary["pnl_pct"] >= 0 else ""
    nsign = "+" if summary["net_pnl_pct"] >= 0 else ""
    lines += [
        f"*Total: ₹{summary['invested_inr']:,.0f} → ₹{summary['value_inr']:,.0f}"
        f"  ({tsign}{summary['pnl_pct']:.1f}%)*",
        f"If sold today: {_signed_inr(summary['net_pnl_inr'])}"
        f" ({nsign}{summary['net_pnl_pct']:.1f}%) after fees & 30% tax",
    ]

    lo_pct = plan["book_profit_min_pct"]
    hi_pct = plan["book_profit_strong_pct"]
    target_lines = []
    for c in summary["coins"]:
        if c["symbol"] in STABLECOINS:
            continue
        lo, hi = sell_targets_inr(portfolio["holdings"][c["symbol"]], plan)
        target_lines.append(f"{c['symbol']}: {_inr(lo)} → {_inr(hi)}")
    if target_lines:
        lines += ["", f"🎯 *Book-profit zone (+{lo_pct:.0f}% to +{hi_pct:.0f}%)*"]
        lines += target_lines

    zone_lines = []
    for sym, sig in sigs.items():
        zone = dip_zone_inr(sig, usd_inr, plan)
        if zone and sym in portfolio["holdings"] and sym not in STABLECOINS:
            zone_lines.append(f"{sym}: {_inr(zone[0])} (buy) / {_inr(zone[1])} (strong buy)")
    if zone_lines:
        tranche = tranche_inr(plan)
        header = "🛒 *Dip-buy zones*"
        if tranche > 0:
            header += f" — next tranche ~₹{tranche:,.0f}"
        lines += ["", header]
        lines += zone_lines

    return "\n".join(lines)


def format_book_profit_alert(
    name: str,
    sym: str,
    quote: dict,
    stats: dict,
    sig: dict,
    plan: dict,
    level: str,
    now: Optional[datetime] = None,
) -> str:
    """Alert when a held coin is inside/above the book-profit band."""
    if now is None:
        now = datetime.now()

    if level == "strong_book":
        headline = f"💰💰 *{name} — Above Your +{plan['book_profit_strong_pct']:.0f}% Target*"
        why = "Gain is above the top of your band — booking here is worth it even if it keeps running."
    else:
        headline = f"💰 *{name} — Book-Profit Zone*"
        why = "Momentum looks stretched — scope for a dip, so this is a reasonable place to book."

    gsign = "+" if stats["pnl_pct"] >= 0 else ""
    return "\n".join([
        headline,
        f"⏰ {now.strftime('%d %b %Y, %H:%M IST')}",
        "",
        f"Price: ₹{quote['price_inr']:,.0f}  (${quote['price_usd']:,.0f})",
        f"Your position: {stats['qty']:g} {sym} @ ₹{stats['avg_cost_inr']:,.0f} avg",
        f"Gain: {gsign}{stats['pnl_pct']:.1f}%  (₹{stats['pnl_inr']:+,.0f})",
        f"If sold now: ₹{stats['net_pnl_inr']:+,.0f} net"
        f" ({stats['net_pnl_pct']:+.1f}%) after fees & 30% tax",
        "",
        f"RSI: {sig['rsi']:.0f}  |  Trend: {sig['trend']}",
        why,
        "",
        "👉 Consider booking part (e.g. half) — keeps the long-term position "
        "and frees cash to re-enter on the next dip.",
    ])


def format_dip_buy_alert(
    name: str,
    sym: str,
    quote: dict,
    sig: dict,
    stats: Optional[dict],
    plan: dict,
    level: str,
    now: Optional[datetime] = None,
) -> str:
    """Alert when a held coin dips into the SMA7 accumulation zone."""
    if now is None:
        now = datetime.now()

    strong = level == "strong_dip"
    headline = (
        f"🛒🛒 *{name} — Strong Dip (accumulate)*" if strong
        else f"🛒 *{name} — Dip-Buy Zone*"
    )
    lines = [
        headline,
        f"⏰ {now.strftime('%d %b %Y, %H:%M IST')}",
        "",
        f"Price: ₹{quote['price_inr']:,.0f}  (${quote['price_usd']:,.0f})",
        f"Now {abs(sig['sma7_gap_pct']):.1f}% below its 7-day average",
        f"RSI: {sig['rsi']:.0f}  |  Trend: {sig['trend']}",
    ]
    if stats:
        lines.append(
            f"Your avg: ₹{stats['avg_cost_inr']:,.0f} — buying here "
            f"{'lowers' if quote['price_inr'] < stats['avg_cost_inr'] else 'raises'} it"
        )
    tranche = tranche_inr(plan)
    if tranche > 0:
        lines += ["", f"👉 Consider deploying one tranche (~₹{tranche:,.0f}) "
                      f"of your ₹{plan['budget_inr']:,.0f} plan."]
    else:
        lines += ["", "👉 Accumulation zone for a long-term position."]
    lines += ["", "⚠️ Deploy in tranches — never the whole budget on one dip."]

    return "\n".join(lines)
