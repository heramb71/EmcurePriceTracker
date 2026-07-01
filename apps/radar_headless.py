"""NSE Trade Opportunity Radar — headless service.

Scans the 6-stock universe on a market-aware schedule, sends gated Telegram
alerts for *manual review only*, persists every fired signal, and re-evaluates
matured outcomes each cycle. Fully isolated from the live trading engine: it
imports only generic data/indicator/alert helpers, never the trading or crypto
code, and never places an order.

Run:  python -m apps.radar_headless
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone

from dotenv import load_dotenv

from src.notify.alerts import send_alert
from src.notify import channels
from src.shared.holidays import is_market_holiday
from src.radar import analytics, scan, scoring, store, tracker
from src.radar.alert_format import format_digest, format_eod_stock, format_opportunity
from src.radar.dispatch import AlertGate

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("radar")

_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_OPEN = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)
_WAKEUP_BEFORE_OPEN = timedelta(minutes=10)
_SEND_RETRY_DELAY_S = 2


# ── scheduler (mirrors main_headless.py; no live-engine import) ──────────────
def _now_ist() -> datetime:
    return datetime.now(_IST)


def _is_market_open(now: datetime | None = None) -> bool:
    now = now or _now_ist()
    if now.weekday() >= 5 or is_market_holiday(now.date()):
        return False
    return _MARKET_OPEN <= now.time() <= _MARKET_CLOSE


def _next_wake_target(now: datetime) -> datetime:
    open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
    wake_today = open_today - _WAKEUP_BEFORE_OPEN
    tradable_today = now.weekday() < 5 and not is_market_holiday(now.date())
    if tradable_today and now < open_today:
        return open_today if now >= wake_today else wake_today
    candidate = open_today + timedelta(days=1)
    while candidate.weekday() >= 5 or is_market_holiday(candidate.date()):
        candidate += timedelta(days=1)
    return candidate - _WAKEUP_BEFORE_OPEN


def _retry_send(send_fn, *args) -> bool:
    if send_fn(*args):
        return True
    time.sleep(_SEND_RETRY_DELAY_S)
    return send_fn(*args)


# ── config ──────────────────────────────────────────────────────────────────
def _refresh_seconds() -> int:
    return int(os.getenv("RADAR_REFRESH_SECONDS", "300"))


def _eod_enabled() -> bool:
    return os.getenv("RADAR_EOD_SUMMARY", "true").lower() == "true"


def _eod_exclude() -> set[str]:
    """Symbols to skip in the EOD digest (default EMCURE — the main tracker
    already sends its own managed end-of-day summary)."""
    raw = os.getenv("RADAR_EOD_EXCLUDE", "EMCURE")
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def send_eod_summaries(tg_token: str, tg_chat: str, exclude: set[str]) -> int:
    """Send one end-of-day summary per universe stock (excluding ``exclude``).

    Runs a fresh scan so the daily bar is finalised, then fans each snapshot out
    as a Telegram message. Returns the number of summaries sent."""
    if not (tg_token and tg_chat):
        return 0
    result = scan.run_scan()
    sent = 0
    for sym, snap in result.snapshots.items():
        if sym.upper() in exclude:
            continue
        if _retry_send(send_alert, tg_token, tg_chat,
                       format_eod_stock(snap, result.regime)):
            sent += 1
            logger.info("EOD summary sent: %s", sym)
    return sent


# ── one scan + dispatch cycle ───────────────────────────────────────────────
def run_cycle(
    conn,
    alert_gate: AlertGate,
    tg_token: str,
    tg_chat: str,
    last_digest: list[datetime],
    digest_minutes: int,
) -> None:
    result = scan.run_scan()
    gated = result.above_gate()  # per-family gate (momentum vs reversion)
    logger.info(
        "Scan: regime=%s breadth=%.2f hits=%d above_gate(mom=%d/rev=%d)=%d illiquid=%s",
        result.regime, result.breadth, len(result.ranked),
        scoring.momentum_gate(), scoring.reversion_gate(), len(gated),
        ",".join(result.illiquid) or "-",
    )

    now = _now_ist().replace(tzinfo=None)
    individual, overflow = alert_gate.select(gated, now)

    tg_ready = bool(tg_token and tg_chat)
    for hit, conf, _rank in individual:
        snap = result.snapshots[hit.stock]
        store.insert_signal(
            conn, stock=hit.stock, signal_type=hit.signal_type, confidence=conf,
            regime=result.regime, price_at_alert=snap.price,
            suggested_stop=hit.stop, suggested_target=hit.target, rr=hit.rr,
        )
        if tg_ready:
            msg = format_opportunity(hit, conf, result.regime, snap.price)
            _retry_send(send_alert, tg_token, tg_chat, msg)

    # Overflow → one digest, throttled to at most once per digest_minutes.
    if overflow and tg_ready:
        due = not last_digest or (now - last_digest[-1]) >= timedelta(minutes=digest_minutes)
        if due:
            _retry_send(send_alert, tg_token, tg_chat,
                        format_digest(overflow, result.regime))
            last_digest.append(now)


def main() -> None:
    if os.getenv("RADAR_ENABLED", "true").lower() != "true":
        logger.info("RADAR_ENABLED is not true — exiting.")
        return

    tg_token, tg_chat = channels.telegram_config("radar")
    if not (tg_token and tg_chat):
        logger.warning("Telegram not configured — alerts will be skipped (scan + DB still run).")

    conn = store.connect()
    alert_gate = AlertGate(
        max_per_day=int(os.getenv("RADAR_MAX_ALERTS_PER_DAY", "12")),
        cooldown_minutes=int(os.getenv("RADAR_COOLDOWN_MINUTES", "90")),
    )
    digest_minutes = int(os.getenv("RADAR_DIGEST_MINUTES", "60"))
    last_digest: list[datetime] = []

    eod_enabled = _eod_enabled()
    eod_exclude = _eod_exclude()
    last_eod_date = None  # date of the last EOD dispatch (once per trading day)

    logger.info("Radar started. gates(mom=%d/rev=%d) refresh=%ds eod=%s",
                scoring.momentum_gate(), scoring.reversion_gate(),
                _refresh_seconds(), eod_enabled)

    while True:
        try:
            if _is_market_open():
                start = time.monotonic()
                run_cycle(conn, alert_gate, tg_token, tg_chat, last_digest, digest_minutes)
                written = tracker.evaluate_due(conn)
                if written:
                    logger.info("Outcomes recorded this cycle: %d", written)
                elapsed = time.monotonic() - start
                time.sleep(max(1.0, _refresh_seconds() - elapsed))
            else:
                # End-of-day per-stock summaries: once, after close, on a
                # trading day (also fires on a restart that lands post-close).
                now = _now_ist()
                is_trading_day = now.weekday() < 5 and not is_market_holiday(now.date())
                if (eod_enabled and is_trading_day
                        and now.time() >= _MARKET_CLOSE
                        and last_eod_date != now.date()):
                    n_eod = send_eod_summaries(tg_token, tg_chat, eod_exclude)
                    logger.info("EOD summaries dispatched: %d", n_eod)
                    last_eod_date = now.date()

                # Daily housekeeping: sweep matured outcomes, then sleep to open.
                written = tracker.evaluate_due(conn)
                if written:
                    logger.info("Off-hours outcome sweep recorded: %d", written)
                logger.info("\n%s", analytics.format_report(conn))
                target = _next_wake_target(_now_ist())
                sleep_secs = max(30.0, (target - _now_ist()).total_seconds())
                logger.info("Market closed. Sleeping %.0f min until %s.",
                            sleep_secs / 60, target.strftime("%Y-%m-%d %H:%M IST"))
                time.sleep(sleep_secs)
        except Exception:
            logger.exception("Radar loop error — continuing after backoff")
            time.sleep(30)


if __name__ == "__main__":
    main()
