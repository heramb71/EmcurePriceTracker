"""
NSE market holiday detection.

Fetches the official NSE holiday master and caches it for the day.
Falls back gracefully if the API is unreachable — never blocks alerts.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_NSE_BASE = "https://www.nseindia.com"
_HOLIDAY_URL = f"{_NSE_BASE}/api/holiday-master?type=trading"
_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.nseindia.com/",
}

_cache: dict[date, str] = {}   # date → holiday description
_cache_year: int | None = None  # year the cache was built for


def _today_ist() -> date:
    return datetime.now(_IST).date()


def _parse_date(s: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _fetch_and_build() -> dict[date, str]:
    session = requests.Session()
    try:
        # Establish session cookies — NSE requires a browser-like session
        session.get(_NSE_BASE, headers=_HEADERS, timeout=10)
        resp = session.get(_HOLIDAY_URL, headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        logger.warning("NSE holiday API unavailable: %s", exc)
        return {}
    finally:
        session.close()

    # API returns a dict keyed by market segment (CM, FO, CD …)
    # CM (Capital Market / equities) is the authoritative segment.
    entries: list[dict] = []
    if isinstance(raw, dict):
        entries = raw.get("CM") or raw.get("equity") or next(iter(raw.values()), [])
    elif isinstance(raw, list):
        entries = raw

    result: dict[date, str] = {}
    for h in entries:
        raw_date = h.get("tradingDate") or h.get("trade_date") or ""
        desc = h.get("description") or h.get("desc") or "Market Holiday"
        d = _parse_date(raw_date)
        if d:
            result[d] = desc

    logger.info("NSE holidays fetched: %d entries for %s", len(result), _today_ist().year)
    return result


def _ensure_cache() -> None:
    global _cache, _cache_year
    year = _today_ist().year
    if _cache_year != year or not _cache:
        data = _fetch_and_build()
        if data:
            _cache = data
            _cache_year = year


def is_market_holiday(today: date | None = None) -> bool:
    """Return True if `today` (IST) is an NSE trading holiday."""
    if today is None:
        today = _today_ist()
    _ensure_cache()
    return today in _cache


def get_holiday_name(today: date | None = None) -> str:
    """Return the holiday description for `today`, or 'Market Holiday'."""
    if today is None:
        today = _today_ist()
    _ensure_cache()
    return _cache.get(today, "Market Holiday")


def format_holiday_alert(ticker: str, today: date | None = None) -> str:
    if today is None:
        today = _today_ist()
    name = get_holiday_name(today)
    dow = today.strftime("%A")
    date_str = today.strftime("%d %b %Y")
    return (
        f"🔴 *NSE Market Closed Today*\n"
        f"\n"
        f"📅 {dow}, {date_str}\n"
        f"🏛️ {name}\n"
        f"\n"
        f"No trading session for {ticker}.NS.\n"
        f"Alerts resume tomorrow at 9:00 AM IST."
    )
