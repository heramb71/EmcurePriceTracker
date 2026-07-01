"""Radar CLI — manual ops + verification.

Usage:
    python -m apps.radar scan-now    # one scan, print ranked table (no alerts/writes)
    python -m apps.radar outcomes     # force a matured-outcome sweep now
    python -m apps.radar report        # print the analytics dashboard
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from src.radar import analytics, scan, scoring, store, tracker
from src.radar.alert_format import signal_label

load_dotenv()


def cmd_scan_now() -> None:
    result = scan.run_scan()
    print(f"Regime: {result.regime}   Breadth: {result.breadth:.2f}   "
          f"Illiquid (excluded): {', '.join(result.illiquid) or '-'}")
    print("-" * 78)
    if not result.ranked:
        print("No setups detected.")
        return
    print(f"{'#':>2}  {'STOCK':<10} {'SIGNAL':<26} {'CONF':>4} {'PRICE':>9} "
          f"{'STOP':>8} {'TARGET':>8} {'RR':>4}  GATE")
    for hit, conf, rank in result.ranked:
        price = result.snapshots[hit.stock].price
        flag = "✓" if scoring.passes_gate(hit.signal_type, conf) else " "
        print(f"{rank:>2}  {hit.stock:<10} {signal_label(hit.signal_type):<26} "
              f"{conf:>4} {price:>9.2f} {hit.stop:>8.2f} {hit.target:>8.2f} "
              f"{hit.rr:>4.1f}   {flag}")


def cmd_outcomes() -> None:
    conn = store.connect()
    written = tracker.evaluate_due(conn)
    print(f"Outcomes recorded: {written}")
    conn.close()


def cmd_report() -> None:
    conn = store.connect()
    print(analytics.format_report(conn))
    conn.close()


_COMMANDS = {
    "scan-now": cmd_scan_now,
    "outcomes": cmd_outcomes,
    "report": cmd_report,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _COMMANDS:
        print(__doc__)
        sys.exit(1)
    _COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
