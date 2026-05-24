from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from emcure_tracker import config
from emcure_tracker.indicators import IndicatorResult
from emcure_tracker.sentiment import SentimentResult
from emcure_tracker.data.market import QuoteData

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertState:
    last_sentiment_label: str
    last_rsi_zone: str          # 'oversold' | 'overbought' | 'normal'


_state: Optional[AlertState] = None


# ── Threshold checks ───────────────────────────────────────────────────────

def _rsi_zone(rsi: float) -> str:
    if rsi <= config.ALERT_RSI_OVERSOLD:
        return "oversold"
    if rsi >= config.ALERT_RSI_OVERBOUGHT:
        return "overbought"
    return "normal"


def check_and_fire(
    quote: QuoteData,
    indicators: IndicatorResult,
    sentiment: SentimentResult,
    send_fn,
) -> AlertState:
    global _state
    messages: list[str] = []
    current_zone = _rsi_zone(indicators.rsi)

    if _state is None:
        _state = AlertState(
            last_sentiment_label=sentiment.label,
            last_rsi_zone=current_zone,
        )
        return _state

    # RSI zone crossing
    if current_zone != _state.last_rsi_zone:
        if current_zone == "oversold":
            messages.append(
                f"📉 RSI OVERSOLD — {config.STOCK_NAME}\n"
                f"RSI: {indicators.rsi} | Price: ₹{quote.price:,.2f}"
            )
        elif current_zone == "overbought":
            messages.append(
                f"📈 RSI OVERBOUGHT — {config.STOCK_NAME}\n"
                f"RSI: {indicators.rsi} | Price: ₹{quote.price:,.2f}"
            )

    # Delivery % spike
    if quote.delivery_pct >= config.ALERT_DELIVERY_PCT_SPIKE:
        messages.append(
            f"📦 HIGH DELIVERY — {config.STOCK_NAME}\n"
            f"Delivery: {quote.delivery_pct:.1f}% | Price: ₹{quote.price:,.2f}"
        )

    # Sentiment flip
    if sentiment.label != _state.last_sentiment_label:
        messages.append(
            f"🔄 SENTIMENT FLIP — {config.STOCK_NAME}\n"
            f"{_state.last_sentiment_label} → {sentiment.label} "
            f"(score: {sentiment.score:+.3f})"
        )

    # Bollinger band touch
    if quote.price <= indicators.bb_lower:
        messages.append(
            f"⬇️ BB LOWER TOUCH — {config.STOCK_NAME}\n"
            f"Price ₹{quote.price:,.2f} ≤ BB Lower ₹{indicators.bb_lower:,.2f}"
        )
    elif quote.price >= indicators.bb_upper:
        messages.append(
            f"⬆️ BB UPPER TOUCH — {config.STOCK_NAME}\n"
            f"Price ₹{quote.price:,.2f} ≥ BB Upper ₹{indicators.bb_upper:,.2f}"
        )

    for msg in messages:
        try:
            send_fn(msg)
        except Exception:
            logger.exception("Alert send failed: %s", msg)

    new_state = AlertState(
        last_sentiment_label=sentiment.label,
        last_rsi_zone=current_zone,
    )
    _state = new_state
    return new_state


# ── Telegram sender ────────────────────────────────────────────────────────

def _make_sender():
    try:
        import requests as _requests

        def _send(text: str) -> None:
            url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
            _requests.post(
                url,
                json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
                timeout=10,
            )

        return _send
    except Exception:
        logger.exception("Could not create Telegram sender")
        return lambda text: None


# ── Alert thread ───────────────────────────────────────────────────────────

def start_alert_thread(stop_event: threading.Event) -> None:
    sender = _make_sender()

    def _loop() -> None:
        # Deferred import avoids circular dependency at module load time
        from emcure_tracker.data import market, nse, news
        from emcure_tracker import indicators, sentiment as sent_mod

        while not stop_event.is_set():
            try:
                ohlcv = market.fetch_ohlcv()
                quote = market.fetch_quote()
                sector = nse.fetch_sector()
                articles = news.fetch_all()

                if ohlcv and quote and articles:
                    ind = indicators.compute_all(
                        ohlcv.df,
                        sector.df if sector else None,
                    )
                    sentiment_result = sent_mod.score_articles(articles)
                    if ind and sentiment_result:
                        check_and_fire(quote, ind, sentiment_result, sender)
            except Exception:
                logger.exception("Alert loop iteration failed")

            stop_event.wait(config.REFRESH_SECONDS)

    thread = threading.Thread(target=_loop, daemon=True, name="alerts")
    thread.start()
