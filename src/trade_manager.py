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

def set_trade(entry: float, qty: int, risk_rupees: float = 4500.0) -> dict:
    """Record a new manual entry. Overwrites any previous trade."""
    sl = round(entry - risk_rupees / qty, 2)
    state = {
        "active":     True,
        "ticker":     os.getenv("TICKER", "EMCURE"),
        "entry":      round(entry, 2),
        "qty":        qty,
        "sl":         sl,
        "t1":         round(entry + 10, 2),
        "t2":         round(entry + 20, 2),
        "t3":         round(entry + 25, 2),
        "levels_hit": [],
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

    Uses day_high for target checks (catches intraday moves between cycles)
    and day_low for SL checks.
    """
    state = _load()
    if not state.get("active"):
        return []

    already_hit = set(state.get("levels_hit", []))
    newly_hit   = []

    checks = [
        ("T1", state["t1"],  day_high >= state["t1"],  "target"),
        ("T2", state["t2"],  day_high >= state["t2"],  "target"),
        ("T3", state["t3"],  day_high >= state["t3"],  "target"),
        ("SL", state["sl"],  day_low  <= state["sl"],  "stoploss"),
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
            })
            already_hit.add(label)

    if newly_hit:
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

    if kind == "stoploss":
        emoji = "🛑"
        header = f"STOP LOSS HIT — ₹{level:,.2f}"
        action = "Exit position immediately."
    elif label == "T1":
        emoji = "🎯"
        header = f"T1 HIT — ₹{level:,.2f}"
        action = "Book half (exit 50%). Move SL to entry."
    elif label == "T2":
        emoji = "🎯🎯"
        header = f"T2 HIT — ₹{level:,.2f}"
        action = "Book remaining or trail to T3."
    else:
        emoji = "🏆"
        header = f"T3 HIT — ₹{level:,.2f}"
        action = "Full exit — target achieved!"

    remaining = []
    if kind == "target":
        if label in ("T1",) and pnl >= 0:
            remaining.append(f"T2 ₹{entry+20:.2f}  T3 ₹{entry+25:.2f}")

    lines = [
        f"{emoji} *{ticker}.NS — {header}*",
        "",
        f"Entry    ₹{entry:,.2f}",
        f"Current  ₹{current_price:,.2f}  (+₹{round(current_price-entry,2):.0f}/sh)",
        f"Qty      {qty} sh",
        f"P&L      ₹{pnl:+,.0f}",
        "",
        f"👉 {action}",
    ]
    if remaining:
        lines += ["", "Remaining targets:"] + remaining

    return "\n".join(lines)
