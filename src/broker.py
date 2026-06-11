from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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
            request_id = r.json()["data"]["request_id"]
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
            logger.info("Kite auto_login step2 (TOTP) ok")

            # Step 3: get request_token — follow redirects and check response body
            r = s.get(
                f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}",
                allow_redirects=False,
                timeout=15,
            )
            request_token = None
            for _ in range(4):
                # Check Location header first
                redirect_url = r.headers.get("location", "")
                logger.info("auto_login hop %d: status=%s location=%s", _, r.status_code, redirect_url[:120] if redirect_url else "(none)")
                # Search both the redirect URL and the response body
                for haystack in (redirect_url, r.text[:4000]):
                    match = re.search(r"request_token=([A-Za-z0-9_\-]+)", haystack)
                    if match:
                        request_token = match.group(1)
                        break
                if request_token:
                    break
                if redirect_url and redirect_url.startswith("http"):
                    r = s.get(redirect_url, allow_redirects=False, timeout=15)
                elif r.status_code == 200:
                    break  # Final page, no more redirects
                else:
                    break
            if not request_token:
                logger.error("auto_login: request_token not found. Last status=%s body_snippet=%s",
                             r.status_code, r.text[:300])
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
