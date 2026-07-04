"""Append-only, per-day decision journal (spec: log every decision, skips included).

Each trading day gets its own JSONL file (``kittybot_journal/kittybot-YYYY-MM-DD.jsonl``)
with one JSON object per line: an ISO timestamp, an event type, and free-form
detail. Append-only + one-object-per-line means a crash can at worst lose the last
line, and the file is trivially greppable/tailable for an audit of *why* the bot
did (or didn't) trade.

Events are also emitted to the standard logger so ``tail -f`` on the service log
shows the same reasoning in real time.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any

logger = logging.getLogger("kittybot.journal")

# Canonical event types (free-form is allowed, but these keep the log searchable).
START = "start"
SKIP_DAY = "skip_day"
DISCARD = "discard"
OBSERVE = "observe"
NO_TRIGGER = "no_trigger"
SELECT = "select"
ENTRY = "entry"
ENTRY_FAILED = "entry_failed"
STOP_MOVED = "stop_moved"
EXIT = "exit"
ERROR = "error"


def _path(journal_dir: str, day: date) -> str:
    return os.path.join(journal_dir, f"kittybot-{day.isoformat()}.jsonl")


def record(journal_dir: str, event: str, detail: dict[str, Any] | None = None,
           *, when: datetime | None = None) -> None:
    """Append one event to today's journal file (best-effort; never raises).

    A journalling failure must not take down the trading loop, so I/O errors are
    logged and swallowed — the same event is always mirrored to the logger.
    """
    when = when or datetime.now()
    row = {"ts": when.isoformat(timespec="seconds"), "event": event, **(detail or {})}
    logger.info("%s %s", event, json.dumps(detail or {}, default=str))
    try:
        os.makedirs(journal_dir, exist_ok=True)
        with open(_path(journal_dir, when.date()), "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.exception("journal write failed for %s", event)


def read_day(journal_dir: str, day: date) -> list[dict[str, Any]]:
    """Return all events logged on ``day`` (empty list if none / unreadable)."""
    path = _path(journal_dir, day)
    rows: list[dict[str, Any]] = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.exception("journal read failed for %s", path)
    return rows
