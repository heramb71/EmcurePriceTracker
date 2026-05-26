"""
Intraday strategy engine — aligned with the user's trading rules:
  - Buy when price opens 20-25 rupees below the 7-day SMA (mean reversion)
  - Buy at open when 7-day trend is clearly Upward (trend follow)
  - Fixed rupee targets: T1=₹10, T2=₹20, T3=₹25
  - No entries when 7-day trend is Downward
  - Hard square-off at 3:20 PM IST; no new entries after 2:30 PM
"""
from __future__ import annotations

from datetime import datetime, time as time_type, timedelta
from typing import Optional

import numpy as np
import pandas as pd

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None  # type: ignore[assignment]

MARKET_OPEN    = time_type(9, 15)
ORB_END_15     = time_type(9, 30)
NOON           = time_type(12, 0)
NO_ENTRY_AFTER = time_type(14, 30)
TIGHTEN_AFTER  = time_type(14, 0)
SQUARE_OFF     = time_type(15, 20)

# Trend thresholds
_SLOPE_PCT_MIN  = 0.20   # slope must be >= 0.20% per day of avg price
_CONSISTENCY    = 0.571  # >= 4 out of 6 day-pairs must move in same direction


# ─────────────────────────────────────────────────────────────────────────────
# 7-day SMA gap
# ─────────────────────────────────────────────────────────────────────────────

def compute_sma7(df: pd.DataFrame) -> float:
    """Simple average of the last 7 closing prices."""
    closes = df["close"].tail(7)
    if len(closes) < 7:
        return round(float(df["close"].mean()), 2)
    return round(float(closes.mean()), 2)


def compute_sma7_gap(price: float, df: pd.DataFrame) -> dict:
    """
    Gap between current price and 7-day SMA (rupees).
    Negative = price is below SMA7 (potential buy).
    """
    sma7 = compute_sma7(df)
    gap = round(price - sma7, 2)
    return {
        "sma7":             sma7,
        "gap":              gap,
        "gap_to_buy_zone":  round(max(0.0, gap + 20.0), 2),  # 0 when already in zone
        "in_buy_zone":      gap <= -20.0,
        "in_strong_zone":   gap <= -25.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7-day trend classification
# ─────────────────────────────────────────────────────────────────────────────

def classify_7d_trend(df: pd.DataFrame) -> str:
    """
    Upward / Downward / Choppy based on last 7 daily closes.

    Upward:   positive slope AND >= 4/6 days moved up
    Downward: negative slope AND >= 4/6 days moved down
    Choppy:   everything else (indecisive or flat)
    """
    closes = df["close"].tail(7).values
    if len(closes) < 4:
        return "Unknown"

    x     = np.arange(len(closes))
    slope = float(np.polyfit(x, closes, 1)[0])

    diffs     = np.diff(closes)
    up_days   = int((diffs > 0).sum())
    down_days = int((diffs < 0).sum())
    total     = len(diffs)

    avg_price   = float(closes.mean())
    slope_pct   = abs(slope) / avg_price * 100
    consistency = max(up_days, down_days) / total

    if slope_pct < _SLOPE_PCT_MIN or consistency < _CONSISTENCY:
        return "Choppy"
    if slope > 0 and up_days > down_days:
        return "Upward"
    if slope < 0 and down_days > up_days:
        return "Downward"
    return "Choppy"


def trend_strength(df: pd.DataFrame) -> float:
    """
    Continuous trend strength: +1.0 (strong up) to -1.0 (strong down).
    Used for whatsapp report text.
    """
    closes = df["close"].tail(7).values
    if len(closes) < 4:
        return 0.0
    x     = np.arange(len(closes))
    slope = float(np.polyfit(x, closes, 1)[0])
    avg   = float(closes.mean())
    # Normalise by price; clamp to [-1, 1]
    return float(np.clip(slope / avg * 100 / 0.5, -1.0, 1.0))


# ─────────────────────────────────────────────────────────────────────────────
# Opening Range Breakout
# ─────────────────────────────────────────────────────────────────────────────

def compute_orb(df_intraday: Optional[pd.DataFrame], minutes: int = 15) -> dict:
    """
    High/low of the first `minutes` minutes after 9:15 AM IST today.
    Returns valid=False when today's bars aren't available yet.
    """
    empty = {"high": 0.0, "low": 0.0, "range": 0.0, "valid": False}
    if df_intraday is None or df_intraday.empty:
        return empty

    df = df_intraday.copy()
    df["_dt"] = pd.to_datetime(df["date"])

    if IST is not None:
        if df["_dt"].dt.tz is None:
            df["_dt"] = df["_dt"].dt.tz_localize("Asia/Kolkata")
        else:
            df["_dt"] = df["_dt"].dt.tz_convert("Asia/Kolkata")
        today = datetime.now(IST).date()
    else:
        today = datetime.utcnow().date()

    today_df = df[df["_dt"].dt.date == today]
    if today_df.empty:
        return empty

    if IST is not None:
        orb_start = IST.localize(datetime.combine(today, MARKET_OPEN))
    else:
        from datetime import timezone
        orb_start = datetime.combine(today, MARKET_OPEN).replace(tzinfo=timezone.utc)

    orb_end = orb_start + timedelta(minutes=minutes)
    orb_df  = today_df[(today_df["_dt"] >= orb_start) & (today_df["_dt"] <= orb_end)]

    if orb_df.empty:
        return empty

    h = round(float(orb_df["high"].max()), 2)
    l = round(float(orb_df["low"].min()), 2)
    return {"high": h, "low": l, "range": round(h - l, 2), "valid": True}


# ─────────────────────────────────────────────────────────────────────────────
# Entry signal
# ─────────────────────────────────────────────────────────────────────────────

def entry_signal(
    price: float,
    sma7_gap: dict,
    trend: str,
    orb: dict,
    now: Optional[datetime] = None,
) -> dict:
    """
    Unified entry decision.

    Returns:
        action:   STRONG_BUY | BUY | WAIT | NO_ENTRY | SQUARE_OFF
        reason:   human-readable string
        strength: 0 (no action) – 3 (strongest)
    """
    if now is None:
        now = datetime.now(IST) if IST else datetime.utcnow()

    t   = now.time()
    gap = sma7_gap.get("gap", 0.0)

    # ── Hard time gates ──────────────────────────────────────────────────────
    if t >= SQUARE_OFF:
        return {"action": "SQUARE_OFF",
                "reason": "3:20 PM — square off all positions",
                "strength": 0}

    if t >= NO_ENTRY_AFTER:
        return {"action": "NO_ENTRY",
                "reason": "No new entries after 2:30 PM",
                "strength": 0}

    # ── Skip strong downtrends (catching a falling knife) ────────────────────
    if trend == "Downward":
        return {"action": "NO_ENTRY",
                "reason": f"7D trend is Downward — skip mean-reversion entry",
                "strength": 0}

    # ── Choppy: wait unless very deep in the zone ────────────────────────────
    if trend == "Choppy" and gap > -25:
        return {"action": "WAIT",
                "reason": f"Choppy trend + gap only ₹{abs(gap):.0f} — wait",
                "strength": 0}

    # ── ORB breakdown + deep gap = strongest signal ──────────────────────────
    orb_valid = orb.get("valid", False)
    if orb_valid and price < orb.get("low", 0.0) and gap <= -20:
        return {"action": "STRONG_BUY",
                "reason": f"ORB breakdown + ₹{abs(gap):.0f} below 7D avg",
                "strength": 3}

    # ── Core mean-reversion zone ──────────────────────────────────────────────
    if sma7_gap.get("in_strong_zone"):
        return {"action": "STRONG_BUY",
                "reason": f"₹{abs(gap):.0f} below 7D SMA — strong mean reversion",
                "strength": 3}

    if sma7_gap.get("in_buy_zone"):
        return {"action": "BUY",
                "reason": f"₹{abs(gap):.0f} below 7D SMA — mean reversion zone",
                "strength": 2}

    # ── Upward trend at open: buy near SMA7 ──────────────────────────────────
    if trend == "Upward" and t <= time_type(9, 35) and -5.0 <= gap <= 5.0:
        return {"action": "BUY",
                "reason": f"Upward trend — entry near SMA7 (gap ₹{gap:+.0f})",
                "strength": 2}

    # ── Approaching buy zone ──────────────────────────────────────────────────
    gap_to_zone = sma7_gap.get("gap_to_buy_zone", 999)
    if gap_to_zone < 10:
        return {"action": "WAIT",
                "reason": f"₹{gap_to_zone:.0f} from buy zone — keep watching",
                "strength": 1}

    return {"action": "WAIT",
            "reason": f"Gap ₹{gap:+.0f} vs SMA7 — not in entry zone",
            "strength": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Position sizing with fixed rupee targets
# ─────────────────────────────────────────────────────────────────────────────

def rupee_targets(entry: float, capital: float, risk_rupees: float) -> Optional[dict]:
    """
    Fixed rupee targets: T1=₹10, T2=₹20, T3=₹25.
    SL derived from max risk: SL = entry - risk_rupees/qty.
    """
    if entry <= 0 or capital <= 0 or risk_rupees <= 0:
        return None

    qty = int(capital / entry)
    if qty <= 0:
        return None

    sl_per  = risk_rupees / qty
    sl      = round(entry - sl_per, 2)

    return {
        "entry":        round(entry, 2),
        "sl":           sl,
        "sl_diff":      round(sl_per, 2),
        "t1":           round(entry + 10, 2),
        "t2":           round(entry + 20, 2),
        "t3":           round(entry + 25, 2),
        "qty":          qty,
        "capital_used": round(qty * entry, 2),
        "max_risk":     round(qty * sl_per, 2),
        "t1_profit":    qty * 10,
        "t2_profit":    qty * 20,
        "t3_profit":    qty * 25,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Time-based position management
# ─────────────────────────────────────────────────────────────────────────────

def time_exit_action(position: Optional[dict], price: float, now: Optional[datetime] = None) -> Optional[dict]:
    """
    Returns a forced exit action based on the clock, or None if no action needed.

    Rules:
      14:00 → tighten stop to T1 (if T1 not yet reached, lock T1 profit)
      15:20 → square off regardless
    """
    if not position:
        return None

    if now is None:
        now = datetime.now(IST) if IST else datetime.utcnow()

    t = now.time()

    if t >= SQUARE_OFF:
        return {"action": "square_off", "reason": "3:20 PM forced exit", "price": price}

    if t >= TIGHTEN_AFTER and not position.get("stop_tightened"):
        entry = float(position.get("entry", 0))
        current_t1 = float(position.get("t1", entry + 10))
        # Move stop to breakeven if not already profitable, or to T1 - 2 if above entry
        new_sl = round(max(entry, current_t1 - 2), 2)
        return {
            "action": "tighten_stop",
            "reason": "2:00 PM — tightening stop to protect gains",
            "new_sl": new_sl,
        }

    return None
