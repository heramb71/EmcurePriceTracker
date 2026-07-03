"""
Liveness heartbeat for headless services.

systemd's ``Restart=`` only catches a process that *exits*. A tracker that
silently wedges — stuck on a network read, deadlocked, or spinning without doing
work — stays "active" forever and no alert is sent, so entries are missed and an
open position goes unmanaged. The fix is a heartbeat the loop refreshes every
cycle plus an external watchdog (``apps/watchdog.py``) that alarms when the
heartbeat goes stale during market hours.

The heartbeat is a single small JSON file written atomically, so a reader never
sees a torn value even if it reads mid-write.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from src.shared.atomic_json import read_json, write_json

# Default lives alongside the other runtime state files at the repo root.
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "heartbeat.json")


def _path(path: Optional[str]) -> str:
    return path or os.getenv("HEARTBEAT_FILE") or _DEFAULT_PATH


def beat(component: str, *, path: Optional[str] = None) -> None:
    """Record that ``component`` is alive right now (epoch seconds + pid)."""
    write_json(_path(path), {"component": component, "ts": time.time(), "pid": os.getpid()})


def last_beat(path: Optional[str] = None) -> dict[str, Any]:
    """Return the last recorded heartbeat, or ``{}`` if none/unreadable."""
    return read_json(_path(path), {})


def age_seconds(path: Optional[str] = None, *, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the last heartbeat, or ``None`` if there is no heartbeat yet."""
    ts = last_beat(path).get("ts")
    if ts is None:
        return None
    return (time.time() if now is None else now) - float(ts)
