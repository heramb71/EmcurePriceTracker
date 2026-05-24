#!/usr/bin/env python3
"""
Emcure Pharmaceuticals — Swing Trader Dashboard
Run: python main.py
"""

# Must be set before any sklearn/joblib import to prevent loky multiprocessing
# segfault on macOS ARM (Apple Silicon) with Python 3.13
import os
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import threading
import logging

from rich.console import Console

from emcure_tracker import config
from emcure_tracker.sentiment import SentimentModel
from emcure_tracker.backtest import run_backtest_background
from emcure_tracker.forecast import init_models_background
from emcure_tracker.alerts import start_alert_thread
from emcure_tracker.display.dashboard import run_dashboard

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

console = Console()


def main() -> None:
    console.print(
        f"\n[bold cyan]🚀 Starting {config.STOCK_NAME} Swing Trader Dashboard...[/bold cyan]\n"
        f"  Refreshing every [bold]{config.REFRESH_SECONDS}s[/bold]. "
        "Press [bold red]Ctrl+C[/bold red] to exit.\n"
    )

    # Shared stop event — signals all background threads to exit cleanly
    stop_event = threading.Event()

    # ── Startup background thread: load FinBERT + train models ────────────
    startup_done = threading.Event()

    def _startup() -> None:
        # Run FinBERT load, model training, and backtest in parallel — they're independent
        t1 = threading.Thread(target=SentimentModel.load, daemon=True, name="finbert")
        t2 = threading.Thread(target=init_models_background, daemon=True, name="models")
        t3 = threading.Thread(target=run_backtest_background, daemon=True, name="backtest")
        for t in (t1, t2, t3):
            t.start()
        for t in (t1, t2, t3):
            t.join()
        startup_done.set()

    threading.Thread(target=_startup, daemon=True, name="startup").start()

    # ── Alert polling thread ───────────────────────────────────────────────
    if config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID:
        start_alert_thread(stop_event)

    # ── Main Rich Live refresh loop ────────────────────────────────────────
    try:
        run_dashboard(startup_done, stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        console.print("\n[bold cyan]👋 Dashboard closed. Happy trading![/bold cyan]\n")


if __name__ == "__main__":
    main()
