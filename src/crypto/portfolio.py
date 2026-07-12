"""
Portfolio-aware buy/sell targets for the crypto tracker.

Reads actual holdings from ``crypto_portfolio.json`` (gitignored — personal
financial data, same treatment as trade_state.json) and turns the generic
BTC/ETH alerts into position-relative ones:

- Per-coin P&L, gross AND net of exchange fees + the Indian VDA tax. The tax
  is applied per coin with NO loss set-off (Sec 115BBH — see costs.py), so a
  losing coin never shelters a winning one.
- Sell targets at the book-profit band (default +20% … +30% over avg cost).
- Dip-buy zones anchored on the 7-day SMA gap — the only trigger that showed
  a real gross edge in the reversion lab (ETH, gap ≥ 7%).
- Tranche sizing for the planned deployment budget (e.g. ₹50k over 6 months).

Everything here is pure computation except ``load_portfolio``, which touches
the filesystem and never raises (missing/malformed file → None).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from src.crypto.costs import post_tax_pct

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "crypto_portfolio.json"
)

# Portfolio symbol → yfinance ticker (quotes for coins beyond BTC/ETH).
YF_SYMBOLS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "DOGE": "DOGE-USD",
    "TUSD": "TUSD-USD",
}

# Stablecoins get P&L display only — no sell targets, no dip zones.
STABLECOINS = {"TUSD", "USDT", "USDC", "DAI"}

DEFAULT_PLAN = {
    "budget_inr":             0.0,   # additional capital to deploy
    "budget_months":          6,
    "book_profit_min_pct":    20.0,  # start of the book-profit band
    "book_profit_strong_pct": 30.0,  # top of the band — alert regardless of dip scope
    "dip_gap_pct":            5.0,   # price this % below SMA7 → accumulation watch
    "strong_dip_gap_pct":     7.0,   # the lab-validated ETH edge threshold
}


def portfolio_path() -> str:
    return os.environ.get("CRYPTO_PORTFOLIO_PATH", _DEFAULT_PATH)


def load_portfolio(path: Optional[str] = None) -> Optional[dict]:
    """Load and validate the portfolio file. Returns None if the file is
    missing or unusable — the tracker then behaves exactly as before."""
    p = path or portfolio_path()
    try:
        with open(p) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("crypto_portfolio.json unreadable — ignoring portfolio")
        return None

    holdings = {}
    for sym, h in (raw.get("holdings") or {}).items():
        try:
            qty = float(h["qty"])
            invested = float(h["invested_inr"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed holding %r", sym)
            continue
        if qty > 0 and invested > 0:
            holdings[sym.upper()] = {"qty": qty, "invested_inr": invested}

    if not holdings:
        return None
    plan = {**DEFAULT_PLAN, **(raw.get("plan") or {})}
    return {"holdings": holdings, "plan": plan}


def avg_cost_inr(holding: dict) -> float:
    return holding["invested_inr"] / holding["qty"]


def holding_stats(sym: str, holding: dict, price_inr: float) -> dict:
    """Gross and net P&L for one coin at the given live INR price.

    ``net_*`` = what actually lands in the bank if sold today: exchange fees
    both sides, then 31.2% tax on any gain (losses get no relief)."""
    invested = holding["invested_inr"]
    value = holding["qty"] * price_inr
    pnl_inr = value - invested
    pnl_pct = pnl_inr / invested * 100
    net_frac = post_tax_pct(pnl_pct / 100)
    return {
        "symbol":       sym,
        "qty":          holding["qty"],
        "invested_inr": invested,
        "avg_cost_inr": avg_cost_inr(holding),
        "price_inr":    price_inr,
        "value_inr":    value,
        "pnl_inr":      pnl_inr,
        "pnl_pct":      pnl_pct,
        "net_pnl_inr":  invested * net_frac,
        "net_pnl_pct":  net_frac * 100,
    }


def sell_targets_inr(holding: dict, plan: dict) -> tuple[float, float]:
    """(band_start, band_top) prices — avg cost lifted by the book-profit band."""
    avg = avg_cost_inr(holding)
    return (
        avg * (1 + plan["book_profit_min_pct"] / 100),
        avg * (1 + plan["book_profit_strong_pct"] / 100),
    )


def tranche_inr(plan: dict) -> float:
    """Suggested size of one dip-buy tranche from the deployment budget."""
    months = max(1, int(plan.get("budget_months", 1)))
    return plan.get("budget_inr", 0.0) / months


def dip_zone_inr(sig: dict, usd_inr: float, plan: dict) -> Optional[tuple[float, float]]:
    """(watch, strong) INR buy-zone prices below the 7-day SMA, or None."""
    sma7 = sig.get("sma7", 0.0)
    if sma7 <= 0:
        return None
    return (
        sma7 * (1 - plan["dip_gap_pct"] / 100) * usd_inr,
        sma7 * (1 - plan["strong_dip_gap_pct"] / 100) * usd_inr,
    )


def dip_level(sig: dict, plan: dict) -> Optional[str]:
    """'strong_dip' / 'dip' when price sits far enough below the 7-day SMA."""
    gap = sig.get("sma7_gap_pct")
    if gap is None:
        return None
    if gap <= -plan["strong_dip_gap_pct"]:
        return "strong_dip"
    if gap <= -plan["dip_gap_pct"]:
        return "dip"
    return None


def should_book_profit(stats: dict, sig: dict, plan: dict) -> Optional[str]:
    """Profit-booking decision for a held coin.

    - ≥ the strong band (default +30%): 'strong_book' — worth booking outright.
    - In the band (default +20…30%): 'book' only when there is scope for a dip
      (overbought RSI or an outright Sell signal) — matches "book 20–30% if a
      dip looks likely", without nagging while momentum is still strong.
    """
    pct = stats["pnl_pct"]
    if pct >= plan["book_profit_strong_pct"]:
        return "strong_book"
    if pct >= plan["book_profit_min_pct"]:
        dip_scope = sig["rsi"] >= 65 or sig["signal"] in ("Sell", "Strong Sell")
        if dip_scope:
            return "book"
    return None


def portfolio_summary(portfolio: dict, prices_inr: dict[str, float]) -> Optional[dict]:
    """Aggregate stats for every holding we have a live price for.

    ``prices_inr`` maps symbol → live INR price; holdings without a price are
    listed under 'missing' so the briefing can say so instead of silently
    dropping them. Totals apply the no-set-off rule: net = Σ per-coin net.
    """
    coins, missing = [], []
    for sym, holding in portfolio["holdings"].items():
        price = prices_inr.get(sym)
        if price and price > 0:
            coins.append(holding_stats(sym, holding, price))
        else:
            missing.append(sym)
    if not coins:
        return None

    invested = sum(c["invested_inr"] for c in coins)
    value = sum(c["value_inr"] for c in coins)
    net_pnl = sum(c["net_pnl_inr"] for c in coins)
    return {
        "coins":        coins,
        "missing":      missing,
        "invested_inr": invested,
        "value_inr":    value,
        "pnl_inr":      value - invested,
        "pnl_pct":      (value - invested) / invested * 100 if invested else 0.0,
        "net_pnl_inr":  net_pnl,
        "net_pnl_pct":  net_pnl / invested * 100 if invested else 0.0,
    }
