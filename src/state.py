from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = Path("strategy_state.json")

# Each target (T1/T2/T3) books this fraction of the remaining position.
PARTIAL_DENOM = 3


def _default_state() -> dict[str, Any]:
    return {
        "position": None,
        "session": {
            "date": None,
            "consecutive_losses": 0,
            "session_pnl": 0.0,
            "halted": False,
        },
        "journal": [],
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return _default_state()
    try:
        with open(path) as f:
            data = json.load(f)
        for key, value in _default_state().items():
            data.setdefault(key, value)
        return data
    except Exception:
        logger.exception("load_state failed; using fresh state")
        return _default_state()


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    try:
        with open(path, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:
        logger.exception("save_state failed")


def reset_session_if_new_day(state: dict[str, Any]) -> dict[str, Any]:
    today = datetime.now().date().isoformat()
    if state["session"].get("date") != today:
        state["session"] = {
            "date": today,
            "consecutive_losses": 0,
            "session_pnl": 0.0,
            "halted": False,
        }
    return state


def open_position(
    state: dict[str, Any], ticker: str, sizing: dict, atr: float
) -> dict[str, Any]:
    state["position"] = {
        "ticker": ticker,
        "entry": sizing["entry"],
        "sl": sizing["sl"],
        "t1": sizing["t1"],
        "t2": sizing.get("t2", round(sizing["entry"] + 20.0, 2)),
        "t3": sizing.get("t3", round(sizing["entry"] + 25.0, 2)),
        "qty": sizing["qty"],
        "qty_remaining": sizing["qty"],
        "atr": round(float(atr), 2),
        "opened_at": datetime.now().isoformat(timespec="seconds"),
        "partial_booked": False,
        "t2_booked": False,
        "t3_booked": False,
        "breakeven_moved": False,
        "partial_pnl": 0.0,
    }
    return state


def book_partial(
    state: dict[str, Any], exit_price: float, reason: str = "t1_hit"
) -> tuple[dict[str, Any], float]:
    """Book 1/3 of remaining position at each target. Move SL to breakeven at T1."""
    pos = state["position"]
    if not pos:
        return state, 0.0

    third = max(1, pos["qty_remaining"] // PARTIAL_DENOM)
    pnl   = round((exit_price - pos["entry"]) * third, 2)

    pos["qty_remaining"] -= third
    pos["partial_pnl"]    = round(float(pos.get("partial_pnl", 0.0)) + pnl, 2)

    if reason == "t1_hit":
        pos["partial_booked"]  = True
        pos["breakeven_moved"] = True
        pos["sl"]              = pos["entry"]
    elif reason == "t2_hit":
        pos["t2_booked"] = True
    elif reason == "t3_hit":
        pos["t3_booked"] = True

    state["session"]["session_pnl"] = round(
        state["session"].get("session_pnl", 0.0) + pnl, 2
    )
    return state, pnl


def close_position(
    state: dict[str, Any], exit_price: float, reason: str
) -> tuple[dict[str, Any], float]:
    pos = state["position"]
    if not pos:
        return state, 0.0

    pnl_remaining = (exit_price - pos["entry"]) * pos["qty_remaining"]
    total_pnl = round(
        float(pos.get("partial_pnl", 0.0)) + pnl_remaining, 2
    )

    trade = {
        "ticker": pos["ticker"],
        "entry": pos["entry"],
        "exit": round(float(exit_price), 2),
        "qty_total": pos["qty"],
        "qty_closed_at_exit": pos["qty_remaining"],
        "partial_booked": pos["partial_booked"],
        "partial_exit_price": pos.get("partial_exit_price"),
        "partial_pnl": pos.get("partial_pnl", 0.0),
        "final_pnl": round(pnl_remaining, 2),
        "total_pnl": total_pnl,
        "reason": reason,
        "opened_at": pos["opened_at"],
        "closed_at": datetime.now().isoformat(timespec="seconds"),
    }
    state["journal"].append(trade)
    state["position"] = None

    session = state["session"]
    session["session_pnl"] = round(
        session.get("session_pnl", 0.0) + pnl_remaining, 2
    )
    if total_pnl < 0:
        session["consecutive_losses"] = session.get("consecutive_losses", 0) + 1
    else:
        session["consecutive_losses"] = 0

    return state, total_pnl


def check_circuit_breaker(
    state: dict[str, Any], capital: float, max_loss_pct: float = 3.0
) -> tuple[bool, str]:
    """Return (halted, reason). Halt if consecutive losses or daily DD breached."""
    session = state["session"]
    if session.get("halted"):
        return True, "manual_halt"
    if session.get("consecutive_losses", 0) >= 2:
        return True, "two_consecutive_losses"
    max_loss = -(capital * max_loss_pct / 100)
    if session.get("session_pnl", 0.0) <= max_loss:
        return True, "daily_drawdown_limit"
    return False, ""
