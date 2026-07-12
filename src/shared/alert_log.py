"""
Persistent dedupe for scheduled and one-shot alerts (shared by services).

``last_alerted`` used to be a plain in-memory dict in the headless loops, so
every mid-day restart — a crash, an OOM, or a deploy via update.sh (which
restarts all services) — forgot what had already been sent and re-fired the
day's briefings and signal alerts (and, for crypto, re-recorded duplicate
rows into the crypto.db outcome evidence base). This wraps the same dict
interface with write-through persistence (atomic temp+rename via atomic_json)
and prunes stale entries on load.

Two retention policies:
  max_age=None       keep only entries from ``now``'s calendar day — right for
                     the EMCURE tracker, whose dedupe keys are all date-scoped.
  max_age=timedelta  keep entries younger than the window — right for crypto,
                     whose ``signal_{sym}`` cooldown keys carry no date and
                     must survive a restart across midnight.

Single-writer by design (only the owning service loop assigns), so no file
lock is needed — readers of the JSON always see a whole file thanks to
``os.replace``. Each service must use its own file.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from src.shared.atomic_json import read_json, write_json

# The EMCURE tracker's default — lives alongside the other runtime state
# files at the repo root (gitignored). Other services pass an explicit path.
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "alerts_sent.json")


def _path(path: Optional[str]) -> str:
    return path or os.getenv("ALERTS_SENT_FILE") or _DEFAULT_PATH


class AlertLog(dict):
    """``dict[str, datetime]`` that persists every assignment to disk.

    Drop-in replacement for the old in-memory ``last_alerted`` dict: keys are
    the dedupe keys, values the aware datetime the alert was sent (cooldown
    checks subtract them from ``now``).
    """

    def __init__(self, now: datetime, path: Optional[str] = None,
                 max_age: Optional[timedelta] = None):
        super().__init__()
        self._file = _path(path)
        raw = read_json(self._file, {})
        if not isinstance(raw, dict):
            raw = {}
        for key, iso in raw.items():
            try:
                ts = datetime.fromisoformat(iso)
                keep = (now - ts) < max_age if max_age is not None \
                    else ts.date() == now.date()
            except (TypeError, ValueError):
                continue   # bad value, or naive/aware mismatch in an edited file
            if keep:
                super().__setitem__(key, ts)

    def __setitem__(self, key: str, value: datetime) -> None:
        super().__setitem__(key, value)
        self._persist()

    def _persist(self) -> None:
        write_json(self._file, {k: v.isoformat() for k, v in self.items()})
