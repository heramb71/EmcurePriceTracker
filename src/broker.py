from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# Shared token file — written by bot_server (TOKEN cmd) or auto_login, read by headless
_TOKEN_FILE = Path("/opt/emcure/kite_token.json")


def _token_file() -> Path:
    """Allow override via env var for local dev."""
    import os
    return Path(os.getenv("KITE_TOKEN_FILE", str(_TOKEN_FILE)))


def _nse_symbol(ticker: str) -> str:
    """EMCURE or EMCURE.NS → EMCURE (bare NSE trading symbol for Kite)."""
    return ticker.replace(".NS", "").upper()


class KiteBroker:
    """
    Thin wrapper around kiteconnect for order placement.

    Auth flow options (in priority order):
      1. auto_login()  — fully headless, needs KITE_USER_ID / PASSWORD / TOTP_SECRET
      2. complete_auth(request_token) — semi-manual: user taps login URL, sends TOKEN cmd
    """

    def __init__(self, api_key: str, api_secret: str):
        from kiteconnect import KiteConnect

        self.api_key    = api_key
        self.api_secret = api_secret
        self.kite       = KiteConnect(api_key=api_key)
        self._load_token()

    # ── Token persistence ────────────────────────────────────────────────────

    def _load_token(self) -> bool:
        tf = _token_file()
        try:
            if not tf.exists():
                return False
            data = json.loads(tf.read_text())
            if data.get("date") != str(date.today()):
                logger.info("Kite token is stale (from %s)", data.get("date"))
                return False
            self.kite.set_access_token(data["access_token"])
            logger.info("Kite token loaded from %s", tf)
            return True
        except Exception:
            logger.exception("Failed to load Kite token from %s", tf)
            return False

    def _save_token(self, access_token: str) -> None:
        tf = _token_file()
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(json.dumps({"access_token": access_token, "date": str(date.today())}))
        logger.info("Kite token saved to %s", tf)

    # ── Authentication ───────────────────────────────────────────────────────

    def login_url(self) -> str:
        return self.kite.login_url()

    def complete_auth(self, request_token: str) -> bool:
        """Exchange Zerodha request_token for a daily access_token."""
        try:
            session_data = self.kite.generate_session(request_token, api_secret=self.api_secret)
            access_token = session_data["access_token"]
            self._save_token(access_token)
            self.kite.set_access_token(access_token)
            logger.warning("Kite auth complete — token saved")
            return True
        except Exception:
            logger.exception("complete_auth failed")
            return False

    def auto_login(self, user_id: str, password: str, totp_secret: str) -> bool:
        """
        Fully automated Kite login via the web session API + TOTP.
        Requires kiteconnect, pyotp, and requests packages.
        """
        try:
            import requests as req
            import pyotp

            s = req.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0"})

            # Step 1: password login
            r = s.post(
                "https://kite.zerodha.com/api/login",
                data={"user_id": user_id, "password": password},
                timeout=15,
            )
            r.raise_for_status()
            resp1 = r.json()
            if resp1.get("status") == "error":
                logger.error("Kite login failed: %s", resp1.get("message"))
                return False
            request_id = resp1["data"]["request_id"]
            logger.info("Kite auto_login step1 ok, request_id=%s", request_id)

            # Step 2: TOTP 2FA
            totp_val = pyotp.TOTP(totp_secret).now()
            r = s.post(
                "https://kite.zerodha.com/api/twofa",
                data={
                    "user_id":     user_id,
                    "request_id":  request_id,
                    "twofa_value": totp_val,
                    "twofa_type":  "totp",
                },
                timeout=15,
            )
            r.raise_for_status()
            resp2 = r.json()
            if resp2.get("status") == "error":
                logger.error("Kite TOTP failed: %s", resp2.get("message"))
                return False
            logger.info("Kite auto_login step2 (TOTP) ok")

            # Step 3: manually follow redirects, stopping as soon as request_token
            # appears in a redirect URL — WITHOUT hitting the callback endpoint.
            # Using allow_redirects=True would cause the requests session to GET
            # the callback URL, which exchanges (and burns) the token on the server
            # before we can use it locally.
            from urllib.parse import urljoin
            request_token = None
            current_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}"
            visited: list[str] = []
            for _ in range(10):
                visited.append(current_url)
                match = re.search(r"request_token=([A-Za-z0-9_\-]+)", current_url)
                if match:
                    request_token = match.group(1)
                    break
                r = s.get(current_url, allow_redirects=False, timeout=15)
                location = r.headers.get("Location", "")
                if location:
                    next_url = urljoin(current_url, location)
                    match = re.search(r"request_token=([A-Za-z0-9_\-]+)", next_url)
                    if match:
                        request_token = match.group(1)
                        visited.append(next_url)
                        break
                    current_url = next_url
                else:
                    break
            logger.info("auto_login redirect chain: %s", " -> ".join(u[:80] for u in visited))
            if not request_token:
                logger.error("auto_login: request_token not found. Visited=%s", visited)
                return False
            logger.info("Kite auto_login got request_token")

            return self.complete_auth(request_token)

        except Exception:
            logger.exception("auto_login failed")
            return False

    def is_authenticated(self) -> bool:
        try:
            self.kite.profile()
            return True
        except Exception:
            return False

    # ── Market data ──────────────────────────────────────────────────────────

    def get_ltp(self, ticker: str) -> float:
        """Last traded price. ticker: EMCURE or EMCURE.NS."""
        instrument = f"NSE:{_nse_symbol(ticker)}"
        try:
            data = self.kite.ltp([instrument])
            return float(data[instrument]["last_price"])
        except Exception:
            logger.exception("get_ltp failed for %s", instrument)
            return 0.0

    def get_position(self, ticker: str) -> Optional[dict]:
        """Return the current intraday (MIS) net position, or None."""
        symbol = _nse_symbol(ticker)
        try:
            positions = self.kite.positions()
            for pos in positions.get("net", []):
                if pos["tradingsymbol"] == symbol and pos["product"] == "MIS":
                    return pos
            return None
        except Exception:
            logger.exception("get_position failed for %s", symbol)
            return None

    # ── Order placement ──────────────────────────────────────────────────────

    def place_market_order(self, ticker: str, qty: int, side: str) -> Optional[str]:
        """
        Place an NSE intraday (MIS) market order.
        side: 'BUY' or 'SELL'
        Returns order_id string, or None on failure.
        """
        from kiteconnect import KiteConnect

        symbol = _nse_symbol(ticker)
        tx = (
            KiteConnect.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else KiteConnect.TRANSACTION_TYPE_SELL
        )
        try:
            order_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=tx,
                quantity=qty,
                product=KiteConnect.PRODUCT_MIS,
                order_type=KiteConnect.ORDER_TYPE_MARKET,
            )
            logger.warning(
                "ORDER PLACED  %s  %s  %d sh  order_id=%s", side, symbol, qty, order_id
            )
            return str(order_id)
        except Exception:
            logger.exception(
                "place_market_order FAILED  %s  %s  qty=%d", side, symbol, qty
            )
            return None


# ── Execution readiness check ────────────────────────────────────────────────

def kite_execution_status(today: date | None = None) -> dict:
    """
    Return a dict describing whether Kite auto-trading will execute today.

    Keys:
      will_execute  bool       — True only when ALL checks pass
      checks        list[dict] — per-check result with name/ok/detail
      summary       str        — human-readable one-liner
    """
    if today is None:
        today = datetime.now(_IST).date()

    checks: list[dict] = []

    # 1. KITE_AUTO_TRADE env var
    auto_trade = os.getenv("KITE_AUTO_TRADE", "false").lower() == "true"
    checks.append({
        "name":   "KITE_AUTO_TRADE",
        "ok":     auto_trade,
        "detail": "enabled" if auto_trade else "disabled (set KITE_AUTO_TRADE=true)",
    })

    # 2. API credentials present
    has_creds = bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_API_SECRET"))
    checks.append({
        "name":   "API credentials",
        "ok":     has_creds,
        "detail": "KITE_API_KEY + KITE_API_SECRET set" if has_creds
                  else "KITE_API_KEY or KITE_API_SECRET missing",
    })

    # 3. Weekday check
    is_weekday = today.weekday() < 5
    day_name = today.strftime("%A")
    checks.append({
        "name":   "Trading day (weekday)",
        "ok":     is_weekday,
        "detail": day_name if is_weekday else f"{day_name} — market closed on weekends",
    })

    # 4. NSE holiday check
    try:
        from src.holidays import is_market_holiday, get_holiday_name
        is_holiday = is_market_holiday(today)
    except Exception:
        is_holiday = False
    not_holiday = not is_holiday
    checks.append({
        "name":   "Not an NSE holiday",
        "ok":     not_holiday,
        "detail": "trading day" if not_holiday else get_holiday_name(today),
    })

    # 5. Kite token valid for today
    tf = _token_file()
    token_ok = False
    token_detail = "token file missing"
    try:
        if tf.exists():
            data = json.loads(tf.read_text())
            if data.get("date") == str(today):
                token_ok = True
                token_detail = f"valid token for {today}"
            else:
                token_detail = f"stale token (from {data.get('date', '?')})"
    except Exception as exc:
        token_detail = f"error reading token: {exc}"
    checks.append({
        "name":   "Kite token (today)",
        "ok":     token_ok,
        "detail": token_detail,
    })

    will_execute = all(c["ok"] for c in checks)
    failing = [c["name"] for c in checks if not c["ok"]]
    if will_execute:
        summary = f"✅ Kite bot WILL execute trades on {today}"
    else:
        summary = f"❌ Kite bot will NOT execute on {today} — {', '.join(failing)}"

    return {"will_execute": will_execute, "checks": checks, "summary": summary}
