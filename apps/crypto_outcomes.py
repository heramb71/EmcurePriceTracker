#!/usr/bin/env python3
"""
Crypto alert outcome report — read-only CLI over crypto.db.

The crypto service records every fired alert and books its 1d/3d/7d forward
outcome (src/crypto/outcomes.py). This prints the evidence:

  python -m apps.crypto_outcomes            # the expectancy report
"""
from __future__ import annotations

import os

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from dotenv import load_dotenv

load_dotenv()

from src.crypto import outcomes


def main() -> None:
    conn = outcomes.connect()
    try:
        print()
        print(outcomes.format_report(conn))
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
