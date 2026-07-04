"""KittyBot — intraday single-stock trader (the spec's "Radar Bot"), headless service.

Loads the daily kitty, waits out the opening range, takes the single strongest
15-minute breakout, and manages it to a 2–5% target with a 1%-risk cap and a hard
15:10 IST exit. Paper-trades by default; places real orders only when
``KITTYBOT_LIVE=true`` and a live broker (``kite``/``upstox``) is configured.

Run:  python -m apps.kittybot_headless
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

from dotenv import load_dotenv

from src.kittybot import journal
from src.kittybot.config import load_config
from src.kittybot.engine import KittyBotEngine
from src.kittybot.notify import make_notifier
from src.shared.holidays import is_market_holiday

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("kittybot")

_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_TICK_SECONDS = 30          # decision cadence during the session
_WAKEUP_BEFORE_OPEN = timedelta(minutes=20)


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _is_trading_day(now: datetime) -> bool:
    return now.weekday() < 5 and not is_market_holiday(now.date())


def _in_session(now: datetime) -> bool:
    return _is_trading_day(now) and _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _next_wake_target(now: datetime) -> datetime:
    """Next time to wake: ~20 min before the next session open."""
    open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
    wake_today = open_today - _WAKEUP_BEFORE_OPEN
    if _is_trading_day(now) and now < open_today:
        return wake_today if now < wake_today else open_today
    candidate = open_today + timedelta(days=1)
    while candidate.weekday() >= 5 or is_market_holiday(candidate.date()):
        candidate += timedelta(days=1)
    return candidate - _WAKEUP_BEFORE_OPEN


def main() -> None:
    cfg = load_config()
    notifier = make_notifier(cfg)
    engine = KittyBotEngine(cfg, notifier=notifier)
    logger.info(
        "KittyBot started. broker=%s live=%s capital=₹%.0f risk=%.1f%% picks=%s alerts=%s",
        cfg.broker, cfg.sends_real_orders, cfg.capital, cfg.risk_per_trade_pct, cfg.picks_path,
        "on" if notifier and notifier.enabled else "off",
    )

    while True:
        try:
            now = _now_ist()
            if _in_session(now):
                engine.step(now)
                time.sleep(_TICK_SECONDS)
            else:
                target = _next_wake_target(now)
                sleep_secs = max(30.0, (target - _now_ist()).total_seconds())
                logger.info("Out of session. Sleeping %.0f min until %s.",
                            sleep_secs / 60, target.strftime("%Y-%m-%d %H:%M IST"))
                time.sleep(sleep_secs)
        except Exception:
            logger.exception("KittyBot loop error — continuing after backoff")
            journal.record(cfg.journal_dir, journal.ERROR, {"note": "loop exception"})
            time.sleep(30)


if __name__ == "__main__":
    main()
