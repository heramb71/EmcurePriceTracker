"""
Zerodha CNC (equity delivery) transaction cost model.

All rates below are the published Zerodha / NSE / statutory charges for equity
*delivery* trades as of 2024-25. Delivery has zero brokerage on Zerodha; the
real costs are STT, exchange transaction charges, GST, SEBI fees, and stamp duty.

Used to convert gross P&L into net P&L so the journal reflects what actually
lands in the account.
"""
from __future__ import annotations

# Securities Transaction Tax — 0.1% on both buy and sell (delivery).
_STT_PCT = 0.001
# NSE exchange transaction charge — 0.00297% on turnover.
_EXCHANGE_TXN_PCT = 0.0000297
# SEBI turnover fee — ₹10 per crore = 0.0001%.
_SEBI_PCT = 0.000001
# Stamp duty — 0.015% on the BUY side only.
_STAMP_PCT = 0.00015
# GST — 18% on (brokerage + exchange txn + SEBI). Brokerage is 0 for delivery.
_GST_PCT = 0.18
# Zerodha brokerage on delivery.
_BROKERAGE = 0.0
# Depository (DP) charge on the SELL side: flat per scrip per day, independent of
# qty — Zerodha ₹13.5 + 18% GST ≈ ₹15.93. It matters most at small capital, where
# it can be the largest single component of a low-quantity trade.
_DP_CHARGE = round(13.5 * (1 + _GST_PCT), 2)


def compute_charges(
    entry: float, exit_price: float, qty: int, include_dp: bool = False
) -> float:
    """
    Total round-trip charges (buy + sell) for a CNC delivery trade, in rupees.

    entry/exit_price are per-share prices; qty is the number of shares.

    include_dp adds the flat DP sell charge (~₹15.93). It defaults to False to
    preserve the original behaviour for existing callers; the swing backtester
    passes include_dp=True so net P&L reflects the true after-cost outcome.
    """
    if entry <= 0 or exit_price <= 0 or qty <= 0:
        return 0.0

    buy_value = entry * qty
    sell_value = exit_price * qty
    turnover = buy_value + sell_value

    stt = (buy_value + sell_value) * _STT_PCT
    exchange_txn = turnover * _EXCHANGE_TXN_PCT
    sebi = turnover * _SEBI_PCT
    stamp = buy_value * _STAMP_PCT
    gst = (_BROKERAGE + exchange_txn + sebi) * _GST_PCT

    total = _BROKERAGE + stt + exchange_txn + sebi + stamp + gst
    if include_dp:
        total += _DP_CHARGE
    return round(total, 2)


def net_pnl(entry: float, exit_price: float, qty: int, gross_pnl: float) -> tuple[float, float]:
    """Return (net_pnl, charges) for a trade given its gross P&L."""
    charges = compute_charges(entry, exit_price, qty)
    return round(gross_pnl - charges, 2), charges
