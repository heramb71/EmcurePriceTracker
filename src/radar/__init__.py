"""NSE Trade Opportunity Radar.

A read-only multi-stock scanner that detects setups across 5 signal types,
scores and ranks them, alerts via Telegram for *manual review only*, and tracks
every signal's forward outcome (MFE/MAE, WIN/LOSS/NEUTRAL) to measure edge.

It never places trades and is fully isolated from the live trading engine.
"""
