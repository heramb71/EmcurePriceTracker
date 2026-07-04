"""Pluggable broker abstraction: Paper (default), Zerodha Kite, and Upstox.

The engine only ever talks to the :class:`Broker` protocol, so swapping venues is
a config change. Every method returns ``None``/empty on failure instead of raising
(matching the codebase convention) so a broker hiccup degrades to "no fill" rather
than crashing the loop.

    Broker (protocol)
      ├── PaperBroker   — simulates fills at the live/last price; the safe default
      ├── KiteBroker    — wraps src.execution.broker.KiteBroker, MIS intraday
      └── UpstoxBroker  — maps the same interface onto the upstox-python-sdk

:func:`make_broker` is the factory the engine calls with a
:class:`~src.kittybot.config.KittyBotConfig`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from src.kittybot.config import KittyBotConfig
from src.shared.data import fetch_live_quote

logger = logging.getLogger(__name__)

BUY = "BUY"
SELL = "SELL"


@dataclass(frozen=True)
class Fill:
    """The result of an order that reached the exchange."""

    order_id: str
    side: str
    qty: int
    price: float
    status: str  # COMPLETE | REJECTED | ...


@runtime_checkable
class Broker(Protocol):
    """The venue interface the engine depends on."""

    name: str

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Last traded price, or ``None`` if unavailable."""
        ...

    def place_market(self, symbol: str, qty: int, side: str, product: str) -> Optional[Fill]:
        """Place a market/near-market order and confirm the fill, or ``None``."""
        ...


# ── Paper ─────────────────────────────────────────────────────────────────────

class PaperBroker:
    """Simulates fills at the current price — the default, risk-free venue.

    ``get_ltp`` uses the shared yfinance live quote (delayed but session-real) so
    paper trading tracks the same prices the engine sees elsewhere. A caller may
    inject a ``price_fn`` (e.g. the engine's cached OR/live price) to fill at an
    exact price in tests or to avoid a redundant fetch.
    """

    name = "paper"

    def __init__(self, price_fn=None):
        self._price_fn = price_fn or (lambda s: (fetch_live_quote(s) or {}).get("price"))
        self._seq = 0

    def get_ltp(self, symbol: str) -> Optional[float]:
        price = self._price_fn(symbol)
        return float(price) if price else None

    def place_market(self, symbol: str, qty: int, side: str, product: str) -> Optional[Fill]:
        price = self.get_ltp(symbol)
        if not price or qty <= 0:
            logger.warning("paper fill skipped: symbol=%s qty=%s price=%s", symbol, qty, price)
            return None
        self._seq += 1
        order_id = f"PAPER-{self._seq}"
        logger.info("PAPER %s %s x%d @ ₹%.2f (%s)", side, symbol, qty, price, product)
        return Fill(order_id=order_id, side=side, qty=qty, price=round(float(price), 2),
                    status="COMPLETE")


# ── Zerodha Kite ──────────────────────────────────────────────────────────────

class KiteBroker:
    """MIS intraday adapter over the project's existing ``execution.broker.KiteBroker``.

    Reuses that class's authenticated session and LTP; places intraday (MIS)
    market-protected limit orders via kiteconnect directly (the existing wrapper
    is CNC-only). Requires ``KITE_API_KEY`` / ``KITE_API_SECRET`` and a valid daily
    token, exactly like the EMCURE engine.
    """

    name = "kite"

    def __init__(self):
        from src.execution.broker import KiteBroker as _Kite
        api_key = os.environ["KITE_API_KEY"]
        api_secret = os.environ["KITE_API_SECRET"]
        self._kite = _Kite(api_key, api_secret)

    def get_ltp(self, symbol: str) -> Optional[float]:
        ltp = self._kite.get_ltp(symbol)
        return ltp if ltp and ltp > 0 else None

    def place_market(self, symbol: str, qty: int, side: str, product: str) -> Optional[Fill]:
        from kiteconnect import KiteConnect

        if not self._kite.is_authenticated():
            logger.error("kite not authenticated — cannot place %s %s", side, symbol)
            return None
        ltp = self.get_ltp(symbol)
        if not ltp or qty <= 0:
            return None
        # Market-protected limit: buy slightly above / sell slightly below LTP.
        slip = 0.001
        price = round(ltp * (1 + slip), 1) if side == BUY else round(ltp * (1 - slip), 1)
        tx = KiteConnect.TRANSACTION_TYPE_BUY if side == BUY else KiteConnect.TRANSACTION_TYPE_SELL
        prod = KiteConnect.PRODUCT_MIS if product == "MIS" else KiteConnect.PRODUCT_CNC
        try:
            order_id = self._kite.kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                exchange=KiteConnect.EXCHANGE_NSE,
                tradingsymbol=symbol.replace(".NS", "").upper(),
                transaction_type=tx,
                quantity=qty,
                product=prod,
                order_type=KiteConnect.ORDER_TYPE_LIMIT,
                price=price,
            )
        except Exception:
            logger.exception("kite place_market failed: %s %s x%d", side, symbol, qty)
            return None
        result = self._kite._await_fill(str(order_id), qty)
        if result["status"] != "COMPLETE":
            return None
        return Fill(order_id=str(order_id), side=side, qty=result["filled_qty"],
                    price=result["fill_price"], status="COMPLETE")


# ── Upstox ────────────────────────────────────────────────────────────────────

class UpstoxBroker:
    """Upstox adapter — same interface, mapped onto the ``upstox-python-sdk``.

    Kept import-guarded so the package installs and tests run without the Upstox
    SDK present. Auth uses ``UPSTOX_ACCESS_TOKEN`` (obtained via Upstox's daily
    OAuth flow, out of band). Order placement targets the intraday (``I``) product.
    """

    name = "upstox"

    def __init__(self):
        try:
            import upstox_client  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "UpstoxBroker requires the 'upstox-python-sdk' package "
                "(pip install upstox-python-sdk) and UPSTOX_ACCESS_TOKEN."
            ) from exc
        import upstox_client

        token = os.environ["UPSTOX_ACCESS_TOKEN"]
        cfg = upstox_client.Configuration()
        cfg.access_token = token
        self._client = upstox_client.ApiClient(cfg)
        self._orders = upstox_client.OrderApi(self._client)
        self._quotes = upstox_client.MarketQuoteApi(self._client)

    @staticmethod
    def _instrument(symbol: str) -> str:
        # Upstox uses "NSE_EQ|<ISIN>" keys; symbol→instrument mapping is deployment
        # specific (loaded from the Upstox instrument master), so it is injected by
        # setting UPSTOX_INSTRUMENT_<SYMBOL>. Keeps this adapter free of a bundled map.
        return os.environ.get(f"UPSTOX_INSTRUMENT_{symbol.upper()}", f"NSE_EQ|{symbol.upper()}")

    def get_ltp(self, symbol: str) -> Optional[float]:  # pragma: no cover - needs live SDK
        try:
            key = self._instrument(symbol)
            resp = self._quotes.ltp(key, api_version="2.0")
            data = getattr(resp, "data", {}) or {}
            for v in data.values():
                return float(v.last_price)
        except Exception:
            logger.exception("upstox get_ltp failed for %s", symbol)
        return None

    def place_market(self, symbol: str, qty: int, side: str, product: str) -> Optional[Fill]:  # pragma: no cover - needs live SDK
        import upstox_client

        if qty <= 0:
            return None
        try:
            body = upstox_client.PlaceOrderRequest(
                quantity=qty,
                product="I" if product == "MIS" else "D",
                validity="DAY",
                price=0.0,
                instrument_token=self._instrument(symbol),
                order_type="MARKET",
                transaction_type=side,
                disclosed_quantity=0,
                trigger_price=0.0,
                is_amo=False,
            )
            resp = self._orders.place_order(body, api_version="2.0")
            order_id = str(resp.data.order_id)
        except Exception:
            logger.exception("upstox place_market failed: %s %s x%d", side, symbol, qty)
            return None
        # Market orders fill immediately; use LTP as the reference fill price.
        price = self.get_ltp(symbol) or 0.0
        return Fill(order_id=order_id, side=side, qty=qty, price=round(price, 2), status="COMPLETE")


# ── factory ───────────────────────────────────────────────────────────────────

def make_broker(cfg: KittyBotConfig, *, price_fn=None) -> Broker:
    """Construct the broker named by ``cfg.broker``.

    Falls back to :class:`PaperBroker` (never real orders) when ``cfg.live`` is
    off, so a live broker is only ever constructed under an explicit live flag.
    """
    if not cfg.sends_real_orders:
        if cfg.broker != "paper":
            logger.warning("kittybot live flag off — forcing PAPER broker (config asked %s)",
                           cfg.broker)
        return PaperBroker(price_fn=price_fn)
    if cfg.broker == "kite":
        return KiteBroker()
    if cfg.broker == "upstox":
        return UpstoxBroker()
    logger.warning("unknown broker %r — using paper", cfg.broker)
    return PaperBroker(price_fn=price_fn)
