"""
Manual trade position tracker.

Records the entry price when you buy, monitors T1/T2/T3 and SL levels,
and fires a WhatsApp alert the first time each level is crossed.

State lives in trade_state.json. The main refresh loop reads it every
cycle — no restart needed after setting a trade via trade.py.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "trade_state.json")


# ─────────────────────────────────────────────────────────────────────────────
# State I/O
# ─────────────────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(state: dict) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def set_trade(
    entry: float,
    qty: int,
    risk_rupees: float = 4500.0,
    *,
    sl: float | None = None,
    t1: float | None = None,
    t2: float | None = None,
    t3: float | None = None,
) -> dict:
    """Record a new manual entry. Overwrites any previous trade.

    Levels default to the intraday convention (SL = entry − risk_rupees/qty,
    T1/T2/T3 = entry + ₹10/₹20/₹25). Pass explicit sl/t1/t2/t3 to override — used
    for delivery/swing positions where the fixed rupee levels don't fit (e.g. a
    percentage-based stop, or a much wider target ladder)."""
    state = {
        "active":     True,
        "ticker":     os.getenv("TICKER", "EMCURE"),
        "entry":      round(entry, 2),
        "qty":        qty,
        "sl":         round(entry - risk_rupees / qty, 2) if sl is None else round(sl, 2),
        "t1":         round(entry + 10, 2) if t1 is None else round(t1, 2),
        "t2":         round(entry + 20, 2) if t2 is None else round(t2, 2),
        "t3":         round(entry + 25, 2) if t3 is None else round(t3, 2),
        "levels_hit": [],
        # high_watermark is intentionally absent here — check_and_mark sets it
        # on the first call so it captures the day's high *at trade entry time*,
        # preventing false alerts from pre-entry intraday highs.
        "opened_at":  datetime.now().isoformat(timespec="seconds"),
    }
    _save(state)
    return state


def clear_trade() -> None:
    """Mark the trade as closed."""
    state = _load()
    state["active"] = False
    state["closed_at"] = datetime.now().isoformat(timespec="seconds")
    _save(state)


def get_trade() -> Optional[dict]:
    """Return the active trade or None."""
    state = _load()
    return state if state.get("active") else None


def check_and_mark(price: float, day_high: float, day_low: float) -> list[dict]:
    """
    Compare current price / day range against trade levels.
    Returns list of newly hit levels (dicts), marks each as hit so it
    only alerts once.

    Uses a high_watermark to prevent false T1/T2/T3 alerts when the day's
    high was already above targets before the trade was entered. On the first
    call after a trade is set, the watermark is initialised to the current
    day_high and no target alerts fire. Subsequent calls only fire when
    day_high exceeds the watermark (i.e. a NEW high was made after entry).
    SL uses day_low and is not subject to the watermark.
    """
    state = _load()
    if not state.get("active"):
        return []

    already_hit = set(state.get("levels_hit", []))
    newly_hit   = []
    state_dirty = False

    # First call after trade is set: calibrate the watermark and skip target checks.
    if "high_watermark" not in state:
        state["high_watermark"] = day_high
        _save(state)
        # Still check SL on first call — it's based on day_low, not the watermark.
        if day_low <= state["sl"] and "SL" not in already_hit:
            pnl = round((state["sl"] - state["entry"]) * state["qty"], 0)
            newly_hit.append({
                "label": "SL",
                "level": state["sl"],
                "kind":  "stoploss",
                "pnl":   int(pnl),
                "entry": state["entry"],
                "qty":   state["qty"],
                "t1":    state["t1"],
                "t2":    state["t2"],
                "t3":    state["t3"],
            })
            already_hit.add("SL")
            state["levels_hit"] = list(already_hit)
            _save(state)
        return newly_hit

    # Only check targets if a new intraday high was made since the watermark was set.
    effective_high = day_high
    if day_high > state["high_watermark"]:
        state["high_watermark"] = day_high
        state_dirty = True
    else:
        # No new high — skip target checks to avoid firing on pre-entry day highs.
        effective_high = state["high_watermark"] - 1  # below all targets

    checks = [
        ("T1", state["t1"], effective_high >= state["t1"], "target"),
        ("T2", state["t2"], effective_high >= state["t2"], "target"),
        ("T3", state["t3"], effective_high >= state["t3"], "target"),
        ("SL", state["sl"], day_low <= state["sl"],        "stoploss"),
    ]

    for label, level, triggered, kind in checks:
        if triggered and label not in already_hit:
            pnl = round((level - state["entry"]) * state["qty"], 0)
            newly_hit.append({
                "label":  label,
                "level":  level,
                "kind":   kind,
                "pnl":    int(pnl),
                "entry":  state["entry"],
                "qty":    state["qty"],
                "t1":     state["t1"],
                "t2":     state["t2"],
                "t3":     state["t3"],
            })
            already_hit.add(label)
            state_dirty = True

    if state_dirty:
        state["levels_hit"] = list(already_hit)
        _save(state)

    return newly_hit


def current_pnl(price: float) -> Optional[dict]:
    """Return live P&L dict for the active trade, or None."""
    state = _load()
    if not state.get("active"):
        return None
    entry = state["entry"]
    qty   = state["qty"]
    pnl   = round((price - entry) * qty, 2)
    return {
        "entry":   entry,
        "qty":     qty,
        "price":   price,
        "pnl":     pnl,
        "pnl_per": round(price - entry, 2),
        "t1":      state["t1"],
        "t2":      state["t2"],
        "t3":      state["t3"],
        "sl":      state["sl"],
        "levels_hit": state.get("levels_hit", []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Alert message formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_target_alert(ticker: str, hit: dict, current_price: float) -> str:
    label   = hit["label"]
    level   = hit["level"]
    entry   = hit["entry"]
    qty     = hit["qty"]
    pnl     = hit["pnl"]
    kind    = hit["kind"]
    # Real per-share moves — never hardcode +₹10/+20/+25, since explicit
    # delivery/swing levels can be anything. Fall back to the level itself when a
    # next-target isn't carried in the hit (older state).
    t2      = hit.get("t2", level)
    t3      = hit.get("t3", level)
    delta   = round(level - entry, 2)

    if kind == "stoploss":
        lines = [
            f"🛑 *Stop Loss Hit — {ticker}*",
            "",
            f"Price fell to ₹{level:,.2f} — our stop loss level.",
            f"Bought at ₹{entry:,.2f}, now at ₹{current_price:,.2f}.",
            f"Loss on {qty} shares: ₹{pnl:+,.0f}",
            "",
            f"👉 Exit your position now to protect capital.",
        ]
    elif label == "T1":
        lines = [
            f"🎯 *First Target Hit — {ticker}*",
            "",
            f"Price reached ₹{level:,.2f}  (+₹{delta:,.2f}/sh from entry ₹{entry:,.2f})",
            f"Profit so far: ₹{pnl:+,.0f} on {qty} shares ✅",
            "",
            f"👉 Sell part of your position now.",
            f"Move stop loss up to ₹{entry:,.2f} — no loss possible now.",
            "",
            f"Next targets: ₹{t2:,.2f} (+₹{round(t2-entry, 2):,.2f})  ·  ₹{t3:,.2f} (+₹{round(t3-entry, 2):,.2f})",
        ]
    elif label == "T2":
        lines = [
            f"🎯🎯 *Second Target Hit — {ticker}*",
            "",
            f"Price reached ₹{level:,.2f}  (+₹{delta:,.2f}/sh from entry ₹{entry:,.2f})",
            f"Profit so far: ₹{pnl:+,.0f} on {qty} shares ✅",
            "",
            f"👉 You can exit remaining shares here,",
            f"or hold for final target ₹{t3:,.2f} (+₹{round(t3-entry, 2):,.2f}).",
        ]
    else:
        lines = [
            f"🏆 *Final Target Hit — {ticker}*",
            "",
            f"Price reached ₹{level:,.2f}  (+₹{delta:,.2f}/sh from entry ₹{entry:,.2f})",
            f"Total profit: ₹{pnl:+,.0f} on {qty} shares 🎉",
            "",
            f"👉 Exit full position. Great trade!",
        ]

    return "\n".join(lines)
