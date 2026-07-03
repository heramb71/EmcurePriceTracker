"""
Persistent per-day dedupe for scheduled and one-shot alerts.

``last_alerted`` used to be a plain in-memory dict in apps/main_headless.py, so
every mid-day restart — a crash, an OOM, or a deploy via update.sh (which
restarts all services) — forgot what had already been sent and re-fired the
day's pre-open briefing or intraday BUY signal. This wraps the same dict
interface with write-through persistence (atomic temp+rename via atomic_json)
and prunes entries from previous days on load: every dedupe key the tracker
uses is date-scoped, so anything older than today is dead weight.

Single-writer by design (only the tracker loop assigns), so no file lock is
needed — readers of the JSON always see a whole file thanks to ``os.replace``.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from src.shared.atomic_json import read_json, write_json

# Lives alongside the other runtime state files at the repo root (gitignored).
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "alerts_sent.json")


def _path(path: Optional[str]) -> str:
    return path or os.getenv("ALERTS_SENT_FILE") or _DEFAULT_PATH


class AlertLog(dict):
    """``dict[str, datetime]`` that persists every assignment to disk.

    Drop-in replacement for the old in-memory ``last_alerted`` dict: keys are
    the per-day dedupe keys, values the aware datetime the alert was sent
    (cooldown checks subtract them from ``now``).
    """

    def __init__(self, now: datetime, path: Optional[str] = None):
        super().__init__()
        self._file = _path(path)
        raw = read_json(self._file, {})
        if not isinstance(raw, dict):
            raw = {}
        for key, iso in raw.items():
            try:
                ts = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                continue
            if ts.date() == now.date():   # prune previous days on load
                super().__setitem__(key, ts)

    def __setitem__(self, key: str, value: datetime) -> None:
        super().__setitem__(key, value)
        self._persist()

    def _persist(self) -> None:
        write_json(self._file, {k: v.isoformat() for k, v in self.items()})
