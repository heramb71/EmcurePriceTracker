#!/usr/bin/env python3
"""
Run the intraday mean-reversion backtest on full historical data.

Usage:
    python run_backtest.py
    python run_backtest.py --ticker RELIANCE --capital 200000
    python run_backtest.py --whatsapp        # also sends report via WhatsApp
    python run_backtest.py --no-skip-downtrend   # include downtrend days too

Options:
    --ticker SYMBOL        NSE ticker (default: EMCURE)
    --capital N            Rupees per trade (default: 100000)
    --risk N               Max risk per trade in rupees (default: 4500)
    --no-skip-downtrend    Allow entries when 7D trend is Downward
    --whatsapp             Send compact report via WhatsApp after printing
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import yfinance as yf

from src.emcure.backtest import format_whatsapp_report, print_report, run_backtest


def _fetch(ticker: str) -> pd.DataFrame:
    print(f"  Downloading {ticker}.NS historical data…", flush=True)
    raw = yf.download(f"{ticker}.NS", period="max", interval="1d", progress=False)
    if raw is None or raw.empty:
        print(f"  ERROR: No data for {ticker}.NS", file=sys.stderr)
        sys.exit(1)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [c.lower() for c in raw.columns]
    raw = raw.reset_index()
    raw.columns = ["date"] + list(raw.columns[1:])
    return raw.sort_values("date").reset_index(drop=True)


def _send_whatsapp(report: str) -> None:
    sid    = os.getenv("TWILIO_ACCOUNT_SID", "")
    token  = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_  = os.getenv("TWILIO_WHATSAPP_FROM", "")
    to     = os.getenv("TWILIO_WHATSAPP_TO", "")

    if not all([sid, token, from_, to]):
        print("\n  [WhatsApp] Missing Twilio credentials — skipping send.")
        return

    from src.notify.alerts import send_whatsapp_alert
    ok = send_whatsapp_alert(sid, token, from_, to, report)
    if ok:
        print("\n  ✅ Backtest report sent via WhatsApp")
    else:
        print("\n  ❌ WhatsApp send failed — check logs")


def main() -> None:
    parser = argparse.ArgumentParser(description="EMCURE intraday backtest")
    parser.add_argument("--ticker",            default="EMCURE")
    parser.add_argument("--capital",  type=float, default=100_000.0)
    parser.add_argument("--risk",     type=float, default=4_500.0)
    parser.add_argument("--no-skip-downtrend", action="store_true")
    parser.add_argument("--whatsapp",          action="store_true")
    args = parser.parse_args()

    print(f"\n🔍 Backtesting {args.ticker} — capital ₹{args.capital:,.0f}"
          f"  risk ₹{args.risk:,.0f}")
    if not args.no_skip_downtrend:
        print("   Downtrend filter: ON (skip entries when 7D trend is Downward)")
    else:
        print("   Downtrend filter: OFF")

    df = _fetch(args.ticker)
    print(f"  Data: {len(df)} trading days  "
          f"({str(df['date'].iloc[0])[:10]} → {str(df['date'].iloc[-1])[:10]})\n")

    result = run_backtest(
        df,
        capital=args.capital,
        risk_rupees=args.risk,
        skip_downtrend=not args.no_skip_downtrend,
    )

    print_report(result, ticker=args.ticker)

    # Show the key strategic insight about downtrend filter
    print("  ⚠️  Key risk: sustained downtrends (like Feb 2025) can produce")
    print("     consecutive stop-outs even with the downtrend filter, because")
    print("     the 7D slope lags the price. Consider adding a wider SMA20")
    print("     filter: only enter if price is above SMA20 OR the downtrend")
    print("     has already lost > 10% from its recent high.\n")

    if args.whatsapp:
        wa_report = format_whatsapp_report(result, ticker=args.ticker)
        _send_whatsapp(wa_report)


if __name__ == "__main__":
    main()
