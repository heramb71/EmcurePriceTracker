"""KittyBot — intraday single-stock NSE trader (the spec's "Radar Bot").

Picks ONE stock per day from a pre-ranked *kitty* (``daily_picks.json``, written
by a separate pre-market screener), waits out the opening range, takes the
strongest 15-minute breakout, and manages the trade to a 2–5% target with a
1%-of-capital risk cap and a hard 15:10 IST exit.

This package is deliberately separate from ``src.radar`` (a read-only multi-stock
*scanner* that never trades). KittyBot places orders — through a pluggable broker
abstraction (paper by default; Zerodha Kite / Upstox for live) — but only when the
``live`` flag is explicitly set. Everything else is paper-traded and journalled.

Design split (so the risk-bearing logic is unit-testable in isolation):
    config        — every threshold, loaded from ``config/kittybot.toml`` + env
    picks         — load/parse the daily kitty (JSON or fallback universe)
    filters       — earnings + opening-gap discards
    opening_range — build the 15-min range, detect breakout triggers
    selection     — pick the single strongest trigger
    risk          — position sizing, target/stop levels, breakeven, exits
    safety        — VIX spike / stale-picks / loss-streak circuit breakers
    state         — crash-safe persistence (open position, loss streak, halt)
    journal       — append-only per-day decision log (skips included)
    broker        — Broker protocol + Paper/Kite/Upstox adapters
    engine        — the impure daily-flow orchestrator that wires it together
"""
