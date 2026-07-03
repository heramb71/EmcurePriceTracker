"""
Dead-man's-switch for the EMCURE tracker.

Run on a short timer (systemd timer / cron). It reads the heartbeat that
apps.main_headless refreshes each loop and, if the heartbeat is stale *during
market hours*, sends a Telegram alarm on the emcure bot. Outside market hours a
stale heartbeat is expected (the loop sleeps until the open), so it stays quiet.

    python -m apps.watchdog          # one check, alert if stale
    WATCHDOG_STALE_SECONDS=900       # staleness threshold (default 15 min)

Exit code is 0 on a healthy check, 1 when an alarm was raised — handy for a
timer's own logging.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from src.emcure import schedule
from src.notify import channels
from src.notify.alerts import send_alert
from src.shared.heartbeat import age_seconds, last_beat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("watchdog")

_IST = timezone(timedelta(hours=5, minutes=30))

# Default threshold: comfortably above the ~5-min refresh so a single slow cycle
# never trips a false alarm, but tight enough to catch a real wedge fast.
_DEFAULT_STALE_SECONDS = 15 * 60


def _now_ist() -> datetime:
    return datetime.now(_IST)


def is_market_hours(now: datetime | None = None) -> bool:
    """True on a trading day between the open and close (IST)."""
    return schedule.is_market_open(now or _now_ist())


def evaluate(age: float | None, in_hours: bool, threshold: float) -> str | None:
    """Return an alarm message if the tracker looks dead, else None.

    Pure decision core so it can be unit-tested without touching the clock,
    the filesystem, or the network.
    """
    if not in_hours:
        return None
    if age is None:
        return (
            "🚨 EMCURE tracker DOWN\n\n"
            "No heartbeat found during market hours — the tracker may not be "
            "running. Check: systemctl status emcure-tracker"
        )
    if age > threshold:
        mins = int(age // 60)
        return (
            "🚨 EMCURE tracker STALLED\n\n"
            f"Last heartbeat was {mins} min ago (limit {int(threshold // 60)} min). "
            "The loop is wedged or the service died. Check: "
            "journalctl -u emcure-tracker -n 50"
        )
    return None


def main() -> int:
    load_dotenv()
    threshold = float(os.getenv("WATCHDOG_STALE_SECONDS", _DEFAULT_STALE_SECONDS))
    in_hours = is_market_hours()
    age = age_seconds()

    alarm = evaluate(age, in_hours, threshold)
    if alarm is None:
        logger.info(
            "OK — market_hours=%s heartbeat_age=%s pid=%s",
            in_hours, None if age is None else f"{age:.0f}s", last_beat().get("pid"),
        )
        return 0

    logger.error("ALARM: %s", alarm.splitlines()[0])
    token, chat_id = channels.telegram_config("emcure")
    if token and chat_id:
        if not send_alert(token, chat_id, alarm):
            logger.error("watchdog alarm failed to send")
    else:
        logger.error("watchdog alarm not sent — no emcure Telegram config")
    return 1


if __name__ == "__main__":
    sys.exit(main())
