"""
India VDA (crypto) transaction-cost and tax model.

Crypto in India is taxed far more harshly than equity delivery, and the rules
reshape what kinds of strategies can work at all:

- **Exchange fee** — typically 0.1%–0.5% per side on Indian exchanges
  (default 0.2%/side here; override per call).
- **30% flat tax on gains + 4% cess = 31.2%**, applied per winning trade.
- **NO loss offset** (Sec 115BBH): a losing trade cannot be set off against
  winning trades — not even between coins, not even in the same year. So the
  tax applies to every winner in full while losers stay full losses. This
  asymmetry is what kills high-frequency round-tripping.
- **1% TDS on every sell** is an *advance* on the 30% tax (creditable at
  filing), not an extra cost, so it is deliberately NOT added here — it is a
  cash-flow drag, not a P&L item. Documented so nobody "fixes" it in.

All functions work in return-fractions (0.05 = +5%) so they compose with the
reversion backtest, which is price-scale-free.
"""
from __future__ import annotations

# Exchange taker fee per side (fraction). 0.002 = 0.2%.
DEFAULT_FEE_PER_SIDE = 0.002

# 30% + 4% health & education cess on the tax.
VDA_TAX_RATE = 0.30 * 1.04  # 0.312


def net_of_fees_pct(gross_pct: float, fee_per_side: float = DEFAULT_FEE_PER_SIDE) -> float:
    """Per-trade return after exchange fees on both legs (approximated as a
    flat drag of two sides on the traded value)."""
    return gross_pct - 2 * fee_per_side


def post_tax_pct(gross_pct: float, fee_per_side: float = DEFAULT_FEE_PER_SIDE) -> float:
    """Per-trade return after fees AND the Indian VDA tax.

    Winners are taxed at 31.2% of the (post-fee) gain; losers get no relief.
    This per-trade application (rather than on the yearly net) is exactly what
    the no-set-off rule mandates.
    """
    net = net_of_fees_pct(gross_pct, fee_per_side)
    if net > 0:
        return net * (1 - VDA_TAX_RATE)
    return net
