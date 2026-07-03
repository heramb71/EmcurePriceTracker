"""
Single source of truth for the scheduled-alert windows.

The pre-open / post-open / EOD window boundaries were inlined as bare
``now.hour == 9 and now.minute < 15`` checks in nine places across apps/main.py
and apps/main_headless.py. That is exactly the split that let the headless
service drift from the dashboard once before (the scheduled logic lived only in
main.py). These pure predicates + daily-key helpers keep the boundaries in one
place so the two entry points can never disagree.

All functions are pure — they take ``now`` (an IST-aware datetime) and never
touch the clock, so they unit-test directly.
"""
from __future__ import annotations

from datetime import datetime

# Alert kinds (also the prefix of their per-day dedupe key).
PRE_OPEN = "pre_open"
POST_OPEN = "post_open"
EOD = "eod"

# Window boundaries, IST wall-clock. The pre-open briefing + holiday notice run
# before the 09:15 open; the post-open update after the opening range forms; the
# EOD summary after the 15:30 close.
_PRE_OPEN_HOUR = 9
_PRE_OPEN_MAX_MINUTE = 15   # [09:00, 09:15)
_POST_OPEN_HOUR = 9
_POST_OPEN_MIN_MINUTE = 20  # [09:20, 10:00)
_EOD_HOUR = 15
_EOD_MIN_MINUTE = 30        # [15:30, 16:00)


def in_pre_open(now: datetime) -> bool:
    """09:00–09:14 — pre-open briefing / holiday notice window."""
    return now.hour == _PRE_OPEN_HOUR and now.minute < _PRE_OPEN_MAX_MINUTE


def in_post_open(now: datetime) -> bool:
    """09:20–09:59 — post-open (opening-range) update window."""
    return now.hour == _POST_OPEN_HOUR and now.minute >= _POST_OPEN_MIN_MINUTE


def in_eod(now: datetime) -> bool:
    """15:30–15:59 — end-of-day summary window."""
    return now.hour == _EOD_HOUR and now.minute >= _EOD_MIN_MINUTE


def daily_key(kind: str, now: datetime) -> str:
    """Per-day dedupe key so each scheduled alert fires once per date."""
    return f"{kind}_{now.date()}"


def due(now: datetime, last_alerted: dict) -> str | None:
    """Return the scheduled alert kind due right now (and not yet sent today).

    Windows are mutually exclusive by construction, so at most one is returned.
    Returns ``None`` when outside every window or the due alert already fired.
    """
    for kind, in_window in (
        (PRE_OPEN, in_pre_open),
        (POST_OPEN, in_post_open),
        (EOD, in_eod),
    ):
        if in_window(now) and daily_key(kind, now) not in last_alerted:
            return kind
    return None
