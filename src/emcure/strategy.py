from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Tuning constants ─────────────────────────────────────────────────────────
# Stop and targets scale with ATR so exits adapt to volatility instead of using
# flat rupee amounts (too tight on busy days, too far on quiet ones).
# EMCURE ATR ≈ ₹30–40 → stop ≈ ₹35, T1/T2/T3 ≈ ₹35/₹70/₹105.
STOP_ATR_MULT = 1.0          # stop = entry − 1.0 × ATR
T1_ATR_MULT   = 1.0          # T1   = entry + 1.0 × ATR
T2_ATR_MULT   = 2.0          # T2   = entry + 2.0 × ATR
T3_ATR_MULT   = 3.0          # T3   = entry + 3.0 × ATR

# Entry-gate thresholds.
RSI_MIN = 40.0
RSI_MAX = 75.0
VOLUME_RATIO_MIN = 0.8


# ────────────────────────────────────────────────────────────────────────────
# Layer 1 — Signal: BUY gate (trend + momentum + volume + regime)
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
    A pass requires trend + momentum + volume + a non-bearish regime. Entries
    are hard-blocked when the regime is Sideways or Trending Down, where the
    Supertrend signal whipsaws.
    """
    price = float(quote.get("price", 0.0))
    ema20 = float(indicators.get("ema20", 0.0))
    rsi = float(indicators.get("rsi", 0.0))
    volume = float(quote.get("volume", 0.0))
    avg_volume = float(indicators.get("avg_volume", 0.0))
    st_value = float(supertrend_row.get("supertrend", 0.0))
    st_direction = int(supertrend_row.get("direction", -1))

    trend_ok = st_direction == 1

    momentum_ok = RSI_MIN < rsi < RSI_MAX

    vol_ratio = (volume / avg_volume) if avg_volume > 0 else 0.0
    volume_ok = vol_ratio > VOLUME_RATIO_MIN

    candle_flags = evaluate_candle(last_candle)
    # Regime is now a hard gate: this is a trend-following strategy, so block
    # entries in Sideways/Trending Down where the Supertrend signal whipsaws.
    regime_ok = regime == "Trending Up"

    conditions = {
        "trend": trend_ok,
        "momentum": momentum_ok,
        "volume": volume_ok,
        "candle": True,
        "regime_ok": regime_ok,
    }

    triggered = trend_ok and momentum_ok and volume_ok and regime_ok

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
# Layer 2 — Sizing: ATR-scaled stop + targets, capital-capped qty
# ────────────────────────────────────────────────────────────────────────────


def compute_position_size(
    capital: float,
    risk_pct: float,
    entry: float,
    atr: float,
    atr_mult: float = STOP_ATR_MULT,
) -> Optional[dict]:
    """
    Compute position sizing with volatility-scaled stop and targets:
      - Stop = entry − atr_mult × ATR
      - T1/T2/T3 = entry + {1,2,3} × ATR  (scales with volatility)
      - Risk amount = capital × risk_pct%
      - Qty = floor(risk amount / risk per share), capped by available capital
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

    t1 = entry + T1_ATR_MULT * atr
    t2 = entry + T2_ATR_MULT * atr
    t3 = entry + T3_ATR_MULT * atr
    return {
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "t1": round(t1, 2),
        "t2": round(t2, 2),
        "t3": round(t3, 2),
        "qty": qty,
        "risk_per_share": round(risk_per_share, 2),
        "risk_amount": round(qty * risk_per_share, 2),
        "capital_used": round(qty * entry, 2),
        "atr": round(atr, 2),
        "atr_mult": atr_mult,
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
      1. Stop hit        → exit_full    (reason: stop_hit)
      2. Supertrend flip → exit_full    (reason: supertrend_exit)
      3. T3 hit          → exit_partial (reason: t3_hit)
      4. T2 hit          → exit_partial (reason: t2_hit)
      5. T1 hit          → exit_partial (reason: t1_hit)
      6. None — hold
    """
    if not position:
        return None

    sl  = float(position["sl"])
    t1  = float(position["t1"])
    t2  = float(position.get("t2", position["t1"] + 10.0))
    t3  = float(position.get("t3", position["t1"] + 15.0))
    t1_booked = bool(position.get("partial_booked"))
    t2_booked = bool(position.get("t2_booked"))
    t3_booked = bool(position.get("t3_booked"))

    if price <= sl:
        return {"action": "exit_full", "reason": "stop_hit", "price": price}

    if supertrend_value > 0 and (price < supertrend_value or supertrend_direction == -1):
        return {"action": "exit_full", "reason": "supertrend_exit", "price": price}

    if not t3_booked and price >= t3:
        return {"action": "exit_partial", "reason": "t3_hit", "price": price}

    if not t2_booked and price >= t2:
        return {"action": "exit_partial", "reason": "t2_hit", "price": price}

    if not t1_booked and price >= t1:
        return {"action": "exit_partial", "reason": "t1_hit", "price": price}

    return None


def unrealised_pnl(position: dict | None, price: float) -> float:
    if not position:
        return 0.0
    qty = int(position.get("qty_remaining", 0))
    entry = float(position.get("entry", 0.0))
    return round((price - entry) * qty, 2)
