from __future__ import annotations

import json
import logging
import os
import re
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# Limit-order price offset from LTP: BUY slightly above, SELL slightly below,
# so the order fills promptly without a true market order (which Kite rejects
# without market protection).
DEFAULT_SLIPPAGE_PCT = 0.1

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
            import pyotp
            import requests as req

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
        """Return the current delivery (CNC) net position, or None."""
        symbol = _nse_symbol(ticker)
        try:
            positions = self.kite.positions()
            for pos in positions.get("net", []):
                if pos["tradingsymbol"] == symbol and pos["product"] == "CNC":
                    return pos
            return None
        except Exception:
            logger.exception("get_position failed for %s", symbol)
            return None

    def held_qty(self, ticker: str) -> Optional[int]:
        """
        Net CNC quantity held at the broker (positions + holdings).
        Returns None only when the broker could not be queried — callers must
        distinguish "broker says zero" (0) from "could not check" (None).
        """
        symbol = _nse_symbol(ticker)
        try:
            qty = 0
            positions = self.kite.positions()
            for pos in positions.get("net", []):
                if pos["tradingsymbol"] == symbol and pos["product"] == "CNC":
                    qty += int(pos.get("quantity") or 0)
            # CNC fills settle into holdings overnight (T+1), so check there too.
            # Freshly bought / just-converted shares sit in t1_quantity until they
            # settle into the demat 'quantity' — count both, or a real delivery
            # holding reads as 0 the day it is opened (false RECONCILE MISMATCH).
            # used_quantity ("quantity sold from the net holding") must ALSO be
            # counted: selling delivery holdings intraday decrements 'quantity'
            # AND books a negative CNC position for the same shares, so without
            # it a routine holdings sale reads as net short (-qty) for the rest
            # of the day and falsely blocks every re-entry (seen live 2026-07-09).
            for h in self.kite.holdings():
                if h.get("tradingsymbol") == symbol:
                    qty += (int(h.get("quantity") or 0)
                            + int(h.get("t1_quantity") or 0)
                            + int(h.get("used_quantity") or 0))
            return qty
        except Exception:
            logger.exception("held_qty failed for %s", symbol)
            return None

    def available_funds(self) -> Optional[float]:
        """
        Live available equity cash for placing CNC orders.
        Returns None when the broker could not be queried so callers can
        distinguish "no funds" (0.0) from "could not check" (None).
        """
        try:
            margins = self.kite.margins()
            equity  = margins.get("equity", {})
            available = equity.get("available", {})
            return float(available.get("live_balance", 0.0))
        except Exception:
            logger.exception("available_funds failed")
            return None

    # ── Order placement ──────────────────────────────────────────────────────

    # Default fill-confirmation window. The main loop refreshes every ~300s,
    # so blocking up to FILL_TIMEOUT_S to confirm a fill is acceptable.
    FILL_TIMEOUT_S = 45
    FILL_POLL_S    = 3

    def _place_limit_order(
        self, ticker: str, qty: int, side: str, slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    ) -> Optional[str]:
        """
        Place an NSE delivery (CNC) limit order with a small slippage buffer.
        Zerodha API does not allow market orders without market protection,
        so we use a limit order priced slightly above LTP (BUY) or below (SELL).
        Returns order_id string, or None on failure. Does NOT confirm the fill —
        use place_order_and_confirm() for anything that mutates trade state.
        """
        from kiteconnect import KiteConnect

        symbol = _nse_symbol(ticker)
        tx = (
            KiteConnect.TRANSACTION_TYPE_BUY
            if side == "BUY"
            else KiteConnect.TRANSACTION_TYPE_SELL
        )
        try:
            ltp = self.get_ltp(ticker)
            if ltp <= 0:
                logger.error("_place_limit_order: could not fetch LTP for %s", symbol)
                return None

            # BUY: bid slightly above LTP to ensure fill; SELL: slightly below
            if side == "BUY":
                limit_price = round(ltp * (1 + slippage_pct / 100), 1)
            else:
                limit_price = round(ltp * (1 - slippage_pct / 100), 1)

            order_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=tx,
                quantity=qty,
                product=KiteConnect.PRODUCT_CNC,
                order_type=KiteConnect.ORDER_TYPE_LIMIT,
                price=limit_price,
            )
            logger.warning(
                "ORDER PLACED  %s  %s  %d sh  limit=₹%.1f  order_id=%s",
                side, symbol, qty, limit_price, order_id,
            )
            return str(order_id)
        except Exception:
            logger.exception(
                "_place_limit_order FAILED  %s  %s  qty=%d", side, symbol, qty
            )
            return None

    def _await_fill(self, order_id: str, qty: int) -> dict:
        """
        Poll order_history until the order is COMPLETE, REJECTED, or CANCELLED.
        On timeout, cancel the still-open order so it cannot fill unattended.
        Returns: {"order_id", "status", "fill_price", "filled_qty"}.
        status is one of COMPLETE / REJECTED / CANCELLED / TIMEOUT / ERROR.
        """
        from kiteconnect import KiteConnect

        deadline    = _time.time() + self.FILL_TIMEOUT_S
        last_status = None
        while _time.time() < deadline:
            try:
                history = self.kite.order_history(order_id)
            except Exception:
                logger.exception("order_history failed for %s", order_id)
                _time.sleep(self.FILL_POLL_S)
                continue

            if history:
                last        = history[-1]
                last_status = last.get("status")
                if last_status == "COMPLETE":
                    fill_price = float(last.get("average_price") or 0.0)
                    filled     = int(last.get("filled_quantity") or qty)
                    logger.warning(
                        "ORDER FILLED  id=%s  qty=%d  avg=₹%.2f",
                        order_id, filled, fill_price,
                    )
                    return {
                        "order_id": order_id,
                        "status": "COMPLETE",
                        "fill_price": fill_price,
                        "filled_qty": filled,
                    }
                if last_status in ("REJECTED", "CANCELLED"):
                    logger.error(
                        "ORDER %s  id=%s  msg=%s",
                        last_status, order_id, last.get("status_message"),
                    )
                    return {
                        "order_id": order_id,
                        "status": last_status,
                        "fill_price": 0.0,
                        "filled_qty": 0,
                    }
            _time.sleep(self.FILL_POLL_S)

        # Timed out still OPEN — cancel so it cannot fill while unmanaged.
        logger.error(
            "ORDER NOT FILLED in %ss  id=%s  last_status=%s — cancelling",
            self.FILL_TIMEOUT_S, order_id, last_status,
        )
        try:
            self.kite.cancel_order(variety=KiteConnect.VARIETY_REGULAR, order_id=order_id)
        except Exception:
            logger.exception("cancel_order failed for %s", order_id)
        return {"order_id": order_id, "status": "TIMEOUT", "fill_price": 0.0, "filled_qty": 0}

    def place_order_and_confirm(
        self, ticker: str, qty: int, side: str, slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    ) -> Optional[dict]:
        """
        Place an order AND wait for confirmation of the fill before returning.

        Returns the fill result dict on a COMPLETE fill, or None if the order
        could not be placed or did not fully fill (rejected / cancelled /
        timed out). Callers MUST only mutate trade state when this returns a
        dict with status == "COMPLETE", using the returned fill_price.
        """
        order_id = self._place_limit_order(ticker, qty, side, slippage_pct)
        if not order_id:
            return None
        result = self._await_fill(order_id, qty)
        if result["status"] != "COMPLETE" or result["filled_qty"] < qty:
            return None
        return result

    # ── Resting stop-loss (exchange-side protection) ──────────────────────────

    def place_stop_loss(
        self, ticker: str, qty: int, trigger_price: float,
        slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    ) -> Optional[str]:
        """Place a RESTING SELL stop-loss (SL) order for a CNC long, so the
        exchange enforces the stop even if the bot/server is offline. The order
        triggers at trigger_price, then becomes a limit sell priced slightly below
        the trigger so it fills on the way down. Returns order_id, or None.

        Zerodha requires the trigger to sit below the live price for a sell SL —
        callers pass a stop that is already below the market."""
        from kiteconnect import KiteConnect

        symbol = _nse_symbol(ticker)
        try:
            trig  = round(trigger_price, 1)
            limit = round(trigger_price * (1 - slippage_pct / 100), 1)   # below trigger for a sell
            order_id = self.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NSE,
                tradingsymbol=symbol,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty,
                product=KiteConnect.PRODUCT_CNC,
                order_type=KiteConnect.ORDER_TYPE_SL,
                trigger_price=trig,
                price=limit,
            )
            logger.warning(
                "STOP PLACED  SELL %s  %d sh  trigger=₹%.1f  limit=₹%.1f  order_id=%s",
                symbol, qty, trig, limit, order_id,
            )
            return str(order_id)
        except Exception:
            logger.exception("place_stop_loss FAILED  %s  qty=%d  trig=%.1f", symbol, qty, trigger_price)
            return None

    def cancel(self, order_id: str) -> bool:
        """Cancel a resting order (e.g. the stop, before exiting at a target)."""
        from kiteconnect import KiteConnect
        try:
            self.kite.cancel_order(variety=KiteConnect.VARIETY_REGULAR, order_id=order_id)
            logger.warning("ORDER CANCELLED  id=%s", order_id)
            return True
        except Exception:
            logger.exception("cancel failed for %s", order_id)
            return False

    def order_state(self, order_id: str) -> Optional[str]:
        """Latest status string for an order (COMPLETE / TRIGGER PENDING / OPEN /
        REJECTED / CANCELLED …), or None if it could not be read."""
        try:
            hist = self.kite.order_history(order_id)
            return hist[-1].get("status") if hist else None
        except Exception:
            logger.exception("order_state failed for %s", order_id)
            return None

    def order_result(self, order_id: str) -> dict:
        """One-shot {status, fill_price, filled_qty} for an order — used to detect
        whether a resting stop has filled and at what price."""
        try:
            hist = self.kite.order_history(order_id)
        except Exception:
            logger.exception("order_result failed for %s", order_id)
            return {"status": None, "fill_price": 0.0, "filled_qty": 0}
        if not hist:
            return {"status": None, "fill_price": 0.0, "filled_qty": 0}
        last = hist[-1]
        return {
            "status":     last.get("status"),
            "fill_price": float(last.get("average_price") or 0.0),
            "filled_qty": int(last.get("filled_quantity") or 0),
        }


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
        from src.shared.holidays import get_holiday_name, is_market_holiday
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
