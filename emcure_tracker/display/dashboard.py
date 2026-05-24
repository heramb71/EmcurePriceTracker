from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from rich.layout import Layout
from rich.live import Live

from emcure_tracker import config, backtest
from emcure_tracker.data import market, nse, news
from emcure_tracker import indicators as ind_mod
from emcure_tracker import sentiment as sent_mod
from emcure_tracker import forecast as fc_mod
from emcure_tracker.display import panels


def _fetch_all() -> dict:
    results: dict = {}

    def _ohlcv():
        return "ohlcv", market.fetch_ohlcv()

    def _quote():
        return "quote", market.fetch_quote()

    def _sector():
        return "sector", nse.fetch_sector()

    def _fii():
        return "fii_dii", nse.fetch_fii_dii()

    def _articles():
        return "articles", news.fetch_all()

    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fn) for fn in (_ohlcv, _quote, _sector, _fii, _articles)]
        for f in as_completed(futures, timeout=config.FETCH_TIMEOUT):
            try:
                key, val = f.result()
                results[key] = val
            except Exception:
                pass

    return results


def _build_layout(data: dict, startup_done: threading.Event) -> Layout:
    quote = data.get("quote")
    ohlcv = data.get("ohlcv")
    sector = data.get("sector")
    fii_dii = data.get("fii_dii")
    articles = data.get("articles")

    df = ohlcv.df if ohlcv else None
    sector_df = sector.df if sector else None

    indicators = ind_mod.compute_all(df, sector_df) if df is not None else None

    sentiment = sent_mod.score_articles(articles) if articles else None

    vol_ratio = 1.0
    if quote and indicators and indicators.avg_volume:
        vol_ratio = quote.volume / indicators.avg_volume

    forecast = None
    if df is not None and indicators and sentiment:
        forecast = fc_mod.compute_forecast(
            df, indicators, sentiment.score if sentiment else 0.0, vol_ratio
        )

    bt_result = backtest.get_result()
    last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=6),
        Layout(name="row1"),
        Layout(name="row2", size=10),
        Layout(name="sentiment", size=16),
        Layout(name="row3"),
    )
    layout["row1"].split_row(
        Layout(name="price"),
        Layout(name="volume"),
        Layout(name="technicals"),
    )
    layout["row2"].split_row(
        Layout(name="fii_dii", ratio=1),
        Layout(name="forecast", ratio=2),
    )
    layout["row3"].split_row(
        Layout(name="backtest"),
    )

    layout["header"].update(panels.make_header(quote, last_updated))
    layout["price"].update(panels.make_price_panel(quote, indicators))
    layout["volume"].update(panels.make_volume_panel(quote, indicators))
    layout["technicals"].update(panels.make_technicals_panel(indicators))
    layout["fii_dii"].update(panels.make_fii_dii_panel(fii_dii))
    layout["forecast"].update(panels.make_forecast_panel(forecast, sentiment))
    layout["sentiment"].update(panels.make_sentiment_panel(sentiment))
    layout["backtest"].update(panels.make_backtest_panel(bt_result))

    return layout


def run_dashboard(startup_done: threading.Event, stop_event: threading.Event) -> None:
    startup_refresh_done = False  # fire one extra render right after startup completes

    with Live(
        panels.make_header(None, "Starting…"),
        refresh_per_second=1,
        screen=True,
    ) as live:
        while not stop_event.is_set():
            data = _fetch_all()
            layout = _build_layout(data, startup_done)
            live.update(layout)

            for _ in range(config.REFRESH_SECONDS):
                if stop_event.is_set():
                    break
                # Break early the first time startup finishes so backtest/models
                # appear immediately instead of waiting up to 300s for next refresh
                if not startup_refresh_done and startup_done.is_set():
                    startup_refresh_done = True
                    break
                time.sleep(1)
