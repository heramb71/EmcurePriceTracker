"""Per-variant trade statistics shared by the research labs (pure).

Takes a list of NET ₹ P&Ls (one per closed round-trip) and returns the numbers
every lab reports: count, win-rate, profit factor, expectancy, total, max
drawdown. A zero-P&L trade counts as a non-win, matching apps/strategy_lab.
"""
from __future__ import annotations

from typing import Sequence


def summarize(nets: Sequence[float]) -> dict:
    """Stats for a sequence of net P&Ls. ``pf`` is ``inf`` with no losers and
    ``None`` with no trades, so callers can tell "no evidence" from "no losses"."""
    n = len(nets)
    if n == 0:
        return {"n": 0, "wins": 0, "win_pct": 0.0, "pf": None,
                "expectancy": 0.0, "net": 0.0, "max_dd": 0.0}
    wins = [x for x in nets if x > 0]
    gross_win = sum(wins)
    gross_loss = -sum(x for x in nets if x <= 0)
    equity = peak = max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": n,
        "wins": len(wins),
        "win_pct": round(100.0 * len(wins) / n, 1),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "expectancy": round(sum(nets) / n, 2),
        "net": round(sum(nets), 2),
        "max_dd": round(max_dd, 2),
    }


def beats(candidate: dict, baseline: dict, min_n: int = 20) -> bool:
    """True only when ``candidate`` has enough trades AND improves both
    expectancy and profit factor over ``baseline`` — the bar a timing tweak must
    clear before it is worth a paper-trading trial, not a live switch."""
    if candidate["n"] < min_n or candidate["pf"] is None:
        return False
    base_pf = baseline["pf"] if baseline["pf"] is not None else 0.0
    return candidate["expectancy"] > baseline["expectancy"] and candidate["pf"] > base_pf
