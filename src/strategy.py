from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Layer 1 — Signal: 4-condition BUY gate
# ────────────────────────────────────────────────────────────────────────────


def evaluate_candle(candle: dict) -> dict:
    """Inspect the last daily candle. Returns descriptive flags."""
    o = float(candle.get("open", 0.0))
    h = float(candle.get("high", 0.0))
    low_ = float(candle.get("low", 0.0))
    c = float(candle.get("close", 0.0))

    rng = h - low_
    body = abs(c - o)
    is_bullish = c > o
    body_ratio = (body / rng) if rng > 0 else 0.0
    close_position = ((c - low_) / rng) if rng > 0 else 0.0

    is_doji = body_ratio < 0.1
    strong_close = close_position >= 0.7
    strong_body = body_ratio >= 0.5

    return {
        "is_bullish": is_bullish,
        "is_doji": is_doji,
        "strong_close": strong_close,
        "strong_body": strong_body,
        "body_ratio": round(body_ratio, 3),
        "close_position": round(close_position, 3),
    }


def check_buy_gate(
    quote: dict,
    indicators: dict,
    supertrend_row: dict,
    last_candle: dict,
    regime: str,
) -> dict:
    """
    Return:
      {
        "triggered": bool,
        "conditions": { "trend", "momentum", "volume", "candle", "regime_ok" },
        "details": {...explanatory data...}
      }
    A pass requires the four primary conditions. Regime is informational.
    """
    price = float(quote.get("price", 0.0))
    ema20 = float(indicators.get("ema20", 0.0))
    rsi = float(indicators.get("rsi", 0.0))
    volume = float(quote.get("volume", 0.0))
    avg_volume = float(indicators.get("avg_volume", 0.0))
    st_value = float(supertrend_row.get("supertrend", 0.0))
    st_direction = int(supertrend_row.get("direction", -1))

    trend_ok = st_direction == 1

    momentum_ok = 40.0 < rsi < 75.0

    vol_ratio = (volume / avg_volume) if avg_volume > 0 else 0.0
    volume_ok = vol_ratio > 0.8

    candle_flags = evaluate_candle(last_candle)
    regime_ok = regime == "Trending Up"

    conditions = {
        "trend": trend_ok,
        "momentum": momentum_ok,
        "volume": volume_ok,
        "candle": True,
        "regime_ok": regime_ok,
    }

    triggered = trend_ok and momentum_ok and volume_ok

    return {
        "triggered": triggered,
        "conditions": conditions,
        "details": {
            "price": price,
            "ema20": ema20,
            "supertrend": st_value,
            "st_direction": st_direction,
            "rsi": rsi,
            "vol_ratio": round(vol_ratio, 2),
            "candle": candle_flags,
            "regime": regime,
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# Layer 2 — Sizing: ATR×2 stop + 1% capital risk
# ────────────────────────────────────────────────────────────────────────────


def compute_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    atr: float,
    atr_mult: float = 0.5,
    rr: float = 1.0,
) -> Optional[dict]:
    """
    Compute position sizing for intraday trades:
      - Stop = entry − atr_mult × ATR  (0.5× ATR ≈ ₹30 stop for EMCURE)
      - Risk amount = capital × risk_pct%
      - Qty = floor(risk amount / risk per share)
      - T1  = entry + rr × risk per share  (1:1 RR keeps T1 realistic intraday)
    Returns None when inputs are invalid.
    """
    if entry <= 0 or atr <= 0 or capital <= 0 or risk_pct <= 0:
        return None

    sl = entry - atr_mult * atr
    risk_per_share = entry - sl
    if risk_per_share <= 0:
        return None

    risk_amount = capital * (risk_pct / 100.0)
    qty = int(risk_amount // risk_per_share)
    qty = min(qty, int(capital // entry))  # never exceed available capital
    if qty <= 0:
        return None

    t1 = entry + rr * risk_per_share
    return {
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "t1": round(t1, 2),
        "qty": qty,
        "risk_per_share": round(risk_per_share, 2),
        "risk_amount": round(qty * risk_per_share, 2),
        "capital_used": round(qty * entry, 2),
        "atr": round(atr, 2),
        "atr_mult": atr_mult,
        "rr": rr,
    }


# ────────────────────────────────────────────────────────────────────────────
# Layer 3 — Execution: manage open position
# ────────────────────────────────────────────────────────────────────────────


def manage_position(
    position: dict[str, Any] | None,
    price: float,
    supertrend_value: float,
    supertrend_direction: int,
) -> Optional[dict]:
    """
    Inspect the current price + Supertrend and return the next action.

    Priority order:
      1. Stop hit       → exit_full   (reason: stop_hit)
      2. Supertrend flip → exit_full  (reason: supertrend_exit)
      3. T1 hit, partial not booked → exit_partial (reason: t1_hit)
      4. None — hold
    """
    if not position:
        return None

    sl = float(position["sl"])
    t1 = float(position["t1"])
    partial_booked = bool(position.get("partial_booked"))

    if price <= sl:
        return {"action": "exit_full", "reason": "stop_hit", "price": price}

    if supertrend_value > 0 and (price < supertrend_value or supertrend_direction == -1):
        return {
            "action": "exit_full",
            "reason": "supertrend_exit",
            "price": price,
        }

    if not partial_booked and price >= t1:
        return {"action": "exit_partial", "reason": "t1_hit", "price": price}

    return None


def unrealised_pnl(position: dict | None, price: float) -> float:
    if not position:
        return 0.0
    qty = int(position.get("qty_remaining", 0))
    entry = float(position.get("entry", 0.0))
    return round((price - entry) * qty, 2)
