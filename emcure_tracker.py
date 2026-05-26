#!/usr/bin/env python3
"""
EMCURE Stock Swing Trader Dashboard
====================================
Live terminal dashboard tracking:
  - Price & Volume (Alpha Vantage)
  - News Sentiment (Google News RSS + VADER)
  - Technical Indicators (RSI, MACD, Bollinger Bands, EMA)
  - Price Forecast for next trading session

Usage:
  1. Set your Alpha Vantage API key below (or via env var ALPHA_VANTAGE_KEY)
     Get a free key at: https://www.alphavantage.co/support/#api-key
  2. Run: python emcure_tracker.py
  3. Press Ctrl+C to exit
"""

import os
import sys
import time
import math
import requests
import feedparser
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

# --- Rich terminal UI ---
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich.align import Align
from rich import box
from rich.style import Style
from rich.progress_bar import ProgressBar
from rich.rule import Rule
from rich.padding import Padding

# --- Sentiment ---
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ─────────────────────────────────────────────
# CONFIGURATION — Edit these
# ─────────────────────────────────────────────
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "YOUR_API_KEY_HERE")
STOCK_SYMBOL = "EMCURE.BSE"          # Alpha Vantage symbol for Emcure on BSE
STOCK_NAME   = "Emcure Pharmaceuticals"
REFRESH_SECONDS = 300                 # Refresh every 5 minutes
NEWS_QUERY = "Emcure Pharmaceuticals stock"
NEWS_RSS_URL = (
    "https://news.google.com/rss/search?q="
    + "+".join(NEWS_QUERY.split())
    + "&hl=en-IN&gl=IN&ceid=IN:en"
)
MAX_NEWS_ITEMS = 10
# ─────────────────────────────────────────────

console = Console()
analyzer = SentimentIntensityAnalyzer()


# ══════════════════════════════════════════════
# MODULE 1 — STOCK DATA (Alpha Vantage)
# ══════════════════════════════════════════════

def fetch_daily_data(symbol: str, api_key: str) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV data from Alpha Vantage (last 100 days)."""
    url = (
        "https://www.alphavantage.co/query"
        f"?function=TIME_SERIES_DAILY"
        f"&symbol={symbol}"
        f"&outputsize=compact"
        f"&apikey={api_key}"
    )
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception as e:
        return None

    ts_key = "Time Series (Daily)"
    if ts_key not in data:
        return None

    rows = []
    for date_str, vals in data[ts_key].items():
        rows.append({
            "date":   date_str,
            "open":   float(vals["1. open"]),
            "high":   float(vals["2. high"]),
            "low":    float(vals["3. low"]),
            "close":  float(vals["4. close"]),
            "volume": int(vals["5. volume"]),
        })
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_intraday_quote(symbol: str, api_key: str) -> Optional[dict]:
    """Fetch latest intraday quote (GLOBAL_QUOTE endpoint)."""
    url = (
        "https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE"
        f"&symbol={symbol}"
        f"&apikey={api_key}"
    )
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception:
        return None

    q = data.get("Global Quote", {})
    if not q:
        return None

    return {
        "price":          float(q.get("05. price", 0)),
        "open":           float(q.get("02. open", 0)),
        "high":           float(q.get("03. high", 0)),
        "low":            float(q.get("04. low", 0)),
        "prev_close":     float(q.get("08. previous close", 0)),
        "change":         float(q.get("09. change", 0)),
        "change_pct":     q.get("10. change percent", "0%"),
        "volume":         int(q.get("06. volume", 0)),
        "latest_trading_day": q.get("07. latest trading day", ""),
    }


# ══════════════════════════════════════════════
# MODULE 2 — TECHNICAL INDICATORS
# ══════════════════════════════════════════════

def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 2) if not rsi.empty else 50.0


def compute_macd(series: pd.Series):
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal    = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal
    return round(macd_line.iloc[-1], 2), round(signal.iloc[-1], 2), round(histogram.iloc[-1], 2)


def compute_bollinger(series: pd.Series, period: int = 20):
    ma   = series.rolling(period).mean()
    std  = series.rolling(period).std()
    upper = ma + 2 * std
    lower = ma - 2 * std
    return round(upper.iloc[-1], 2), round(ma.iloc[-1], 2), round(lower.iloc[-1], 2)


def compute_ema(series: pd.Series, span: int) -> float:
    return round(series.ewm(span=span, adjust=False).mean().iloc[-1], 2)


def compute_avg_volume(df: pd.DataFrame, days: int = 20) -> int:
    return int(df["volume"].tail(days).mean())


def volume_signal(current_vol: int, avg_vol: int) -> str:
    ratio = current_vol / avg_vol if avg_vol else 1
    if ratio >= 2.0:
        return f"[bold green]🔥 Very High ({ratio:.1f}x avg)[/bold green]"
    elif ratio >= 1.5:
        return f"[green]↑ High ({ratio:.1f}x avg)[/green]"
    elif ratio >= 0.8:
        return f"[yellow]→ Normal ({ratio:.1f}x avg)[/yellow]"
    else:
        return f"[red]↓ Low ({ratio:.1f}x avg)[/red]"


def rsi_signal(rsi: float) -> tuple[str, str]:
    if rsi >= 70:
        return "Overbought", "red"
    elif rsi <= 30:
        return "Oversold", "green"
    elif rsi >= 60:
        return "Bullish", "yellow"
    elif rsi <= 40:
        return "Bearish", "cyan"
    else:
        return "Neutral", "white"


# ══════════════════════════════════════════════
# MODULE 3 — NEWS SENTIMENT
# ══════════════════════════════════════════════

def fetch_news(rss_url: str, max_items: int = 10) -> list[dict]:
    """Fetch news from Google News RSS and score sentiment."""
    try:
        feed = feedparser.parse(rss_url)
    except Exception:
        return []

    articles = []
    for entry in feed.entries[:max_items]:
        title   = entry.get("title", "")
        summary = entry.get("summary", "")
        pub     = entry.get("published", "")

        text = f"{title}. {summary}"
        scores = analyzer.polarity_scores(text)
        compound = scores["compound"]

        if compound >= 0.05:
            sentiment = "Bullish"
            color     = "green"
            icon      = "▲"
        elif compound <= -0.05:
            sentiment = "Bearish"
            color     = "red"
            icon      = "▼"
        else:
            sentiment = "Neutral"
            color     = "yellow"
            icon      = "●"

        articles.append({
            "title":     title[:80] + ("…" if len(title) > 80 else ""),
            "sentiment": sentiment,
            "color":     color,
            "icon":      icon,
            "compound":  round(compound, 3),
            "published": pub[:25] if pub else "–",
        })

    return articles


def aggregate_sentiment(articles: list[dict]) -> dict:
    """Aggregate individual scores into an overall sentiment."""
    if not articles:
        return {"label": "No Data", "score": 0.0, "color": "dim", "bullish": 0, "bearish": 0, "neutral": 0}

    scores    = [a["compound"] for a in articles]
    avg_score = sum(scores) / len(scores)
    bullish   = sum(1 for a in articles if a["sentiment"] == "Bullish")
    bearish   = sum(1 for a in articles if a["sentiment"] == "Bearish")
    neutral   = sum(1 for a in articles if a["sentiment"] == "Neutral")

    if avg_score >= 0.15:
        label, color = "Strongly Bullish", "bold green"
    elif avg_score >= 0.05:
        label, color = "Bullish", "green"
    elif avg_score <= -0.15:
        label, color = "Strongly Bearish", "bold red"
    elif avg_score <= -0.05:
        label, color = "Bearish", "red"
    else:
        label, color = "Neutral", "yellow"

    return {
        "label":   label,
        "score":   round(avg_score, 3),
        "color":   color,
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
    }


# ══════════════════════════════════════════════
# MODULE 4 — PRICE FORECAST ENGINE
# ══════════════════════════════════════════════

def forecast_price(df: pd.DataFrame, quote: dict, sentiment: dict) -> dict:
    """
    Combine technicals + sentiment to forecast next session's price range.

    Method:
    - Base: EMA20 / current price
    - RSI bias: oversold → positive push, overbought → negative
    - MACD bias: histogram direction
    - Sentiment bias: compound score * weight
    - Volatility: average daily range over last 10 days
    - Bollinger squeeze: price near band = mean reversion signal
    """
    close = df["close"]
    rsi   = compute_rsi(close)
    macd_line, signal_line, hist = compute_macd(close)
    bb_upper, bb_mid, bb_lower   = compute_bollinger(close)
    ema20 = compute_ema(close, 20)
    ema50 = compute_ema(close, 50)

    current = quote["price"] if quote["price"] else close.iloc[-1]
    prev_close = quote["prev_close"] if quote.get("prev_close") else close.iloc[-2]

    # Average daily range (volatility proxy)
    df["range"] = df["high"] - df["low"]
    avg_range = df["range"].tail(10).mean()
    avg_range_pct = avg_range / current * 100

    # --- Bias components (each returns a % nudge) ---
    # 1. RSI bias
    rsi_bias = 0.0
    if rsi < 30:
        rsi_bias = +0.5       # strongly oversold → bounce expected
    elif rsi < 40:
        rsi_bias = +0.25
    elif rsi > 70:
        rsi_bias = -0.5       # overbought → pullback
    elif rsi > 60:
        rsi_bias = -0.25

    # 2. MACD bias
    macd_bias = 0.0
    if hist > 0 and macd_line > signal_line:
        macd_bias = +0.3
    elif hist < 0 and macd_line < signal_line:
        macd_bias = -0.3

    # 3. Trend bias (EMA cross)
    trend_bias = 0.0
    if ema20 > ema50:
        trend_bias = +0.2
    elif ema20 < ema50:
        trend_bias = -0.2

    # 4. Bollinger bias (mean reversion)
    bb_bias = 0.0
    if current < bb_lower:
        bb_bias = +0.4        # price below lower band → likely bounce up
    elif current > bb_upper:
        bb_bias = -0.4        # price above upper band → likely pullback

    # 5. Sentiment bias
    sent_bias = sentiment["score"] * 0.8   # scale: ±0.8%

    # 6. Volume bias (handled separately — volume > avg boosts conviction)
    vol_ratio = quote["volume"] / compute_avg_volume(df) if quote.get("volume") else 1.0
    conviction_mult = min(1.5, 1.0 + (vol_ratio - 1.0) * 0.3)

    # --- Combine ---
    total_bias_pct = (rsi_bias + macd_bias + trend_bias + bb_bias + sent_bias) * conviction_mult
    forecast_mid   = current * (1 + total_bias_pct / 100)

    # Range: use avg_range as width, scaled by bias conviction
    half_range = avg_range * 0.6
    forecast_low  = round(forecast_mid - half_range, 2)
    forecast_high = round(forecast_mid + half_range, 2)
    forecast_mid  = round(forecast_mid, 2)

    # Signal label
    if total_bias_pct >= 1.0:
        signal = "Strong Buy"
        sig_color = "bold green"
    elif total_bias_pct >= 0.3:
        signal = "Buy"
        sig_color = "green"
    elif total_bias_pct <= -1.0:
        signal = "Strong Sell"
        sig_color = "bold red"
    elif total_bias_pct <= -0.3:
        signal = "Sell"
        sig_color = "red"
    else:
        signal = "Hold / Wait"
        sig_color = "yellow"

    return {
        "mid":         forecast_mid,
        "low":         forecast_low,
        "high":        forecast_high,
        "bias_pct":    round(total_bias_pct, 3),
        "avg_range":   round(avg_range, 2),
        "rsi":         rsi,
        "macd":        macd_line,
        "signal_line": signal_line,
        "hist":        hist,
        "bb_upper":    bb_upper,
        "bb_mid":      bb_mid,
        "bb_lower":    bb_lower,
        "ema20":       ema20,
        "ema50":       ema50,
        "conviction":  round(conviction_mult, 2),
        "signal":      signal,
        "sig_color":   sig_color,
        "vol_ratio":   round(vol_ratio, 2),
    }


# ══════════════════════════════════════════════
# MODULE 5 — RICH DASHBOARD
# ══════════════════════════════════════════════

def make_header(quote: dict, last_updated: str) -> Panel:
    price = quote.get("price", 0)
    change = quote.get("change", 0)
    pct    = quote.get("change_pct", "0%")
    day    = quote.get("latest_trading_day", "")

    arrow = "▲" if change >= 0 else "▼"
    color = "green" if change >= 0 else "red"

    text = Text(justify="center")
    text.append(f"\n  {STOCK_NAME}  ({STOCK_SYMBOL})\n", style="bold white")
    text.append(f"  ₹{price:,.2f}  ", style=f"bold {color} on default")
    text.append(f"  {arrow} {change:+.2f}  ({pct})  ", style=color)
    text.append(f"  {day}  ", style="dim")
    text.append(f"\n  Last updated: {last_updated}  ", style="dim italic")

    return Panel(Align.center(text), style="bold blue", box=box.DOUBLE_EDGE)


def make_price_panel(quote: dict, forecast: dict) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Metric", style="dim", width=20)
    tbl.add_column("Value", justify="right")

    price = quote.get("price", 0)
    tbl.add_row("Current Price",  f"[bold white]₹{price:,.2f}[/bold white]")
    tbl.add_row("Open",           f"₹{quote.get('open', 0):,.2f}")
    tbl.add_row("High",           f"[green]₹{quote.get('high', 0):,.2f}[/green]")
    tbl.add_row("Low",            f"[red]₹{quote.get('low', 0):,.2f}[/red]")
    tbl.add_row("Prev Close",     f"₹{quote.get('prev_close', 0):,.2f}")
    tbl.add_row("Day Range",
                f"₹{quote.get('low',0):,.0f} – ₹{quote.get('high',0):,.0f}  "
                f"(Δ {quote.get('high',0)-quote.get('low',0):.1f})")
    tbl.add_row("Avg Day Range",  f"₹{forecast['avg_range']:,.2f}")
    tbl.add_row("", "")
    tbl.add_row("EMA 20",         f"₹{forecast['ema20']:,.2f}")
    tbl.add_row("EMA 50",         f"₹{forecast['ema50']:,.2f}")
    tbl.add_row("BB Upper",       f"[red]₹{forecast['bb_upper']:,.2f}[/red]")
    tbl.add_row("BB Middle",      f"₹{forecast['bb_mid']:,.2f}")
    tbl.add_row("BB Lower",       f"[green]₹{forecast['bb_lower']:,.2f}[/green]")

    return Panel(tbl, title="[bold]📊 Price & Bands[/bold]", border_style="blue")


def make_volume_panel(quote: dict, forecast: dict) -> Panel:
    vol = quote.get("volume", 0)
    avg = int(quote.get("volume", 0) / forecast["vol_ratio"]) if forecast["vol_ratio"] else 1
    ratio = forecast["vol_ratio"]

    bar_len = 30
    filled  = min(bar_len, int(bar_len * min(ratio, 3.0) / 3.0))
    bar     = "█" * filled + "░" * (bar_len - filled)
    bar_color = "green" if ratio >= 1.5 else ("yellow" if ratio >= 0.8 else "red")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Metric", style="dim", width=20)
    tbl.add_column("Value", justify="right")

    tbl.add_row("Today's Volume",     f"[bold white]{vol:,}[/bold white]")
    tbl.add_row("20-day Avg Volume",  f"{avg:,}")
    tbl.add_row("Vol vs Avg",         f"[{bar_color}]{ratio:.2f}x[/{bar_color}]")
    tbl.add_row("Volume Bar",         f"[{bar_color}]{bar}[/{bar_color}]")
    tbl.add_row("Conviction Mult",    f"{forecast['conviction']:.2f}x")

    return Panel(tbl, title="[bold]📦 Volume Analysis[/bold]", border_style="cyan")


def make_technicals_panel(forecast: dict) -> Panel:
    rsi = forecast["rsi"]
    rsi_label, rsi_color = rsi_signal(rsi)

    hist = forecast["hist"]
    macd_color = "green" if hist > 0 else "red"
    macd_arrow = "▲" if hist > 0 else "▼"

    ema_cross = "EMA20 > EMA50 (Bullish)" if forecast["ema20"] > forecast["ema50"] else "EMA20 < EMA50 (Bearish)"
    ema_color = "green" if forecast["ema20"] > forecast["ema50"] else "red"

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Indicator", style="dim", width=20)
    tbl.add_column("Value", justify="right")
    tbl.add_column("Signal", justify="center")

    tbl.add_row(
        "RSI (14)",
        f"{rsi}",
        f"[{rsi_color}]{rsi_label}[/{rsi_color}]"
    )
    tbl.add_row(
        "MACD",
        f"{forecast['macd']:.2f}",
        f"[{macd_color}]{macd_arrow} {forecast['macd']:.2f} / {forecast['signal_line']:.2f}[/{macd_color}]"
    )
    tbl.add_row(
        "MACD Histogram",
        f"{hist:.2f}",
        f"[{macd_color}]{'Positive momentum' if hist > 0 else 'Negative momentum'}[/{macd_color}]"
    )
    tbl.add_row(
        "EMA Cross",
        f"{forecast['ema20']:.2f} / {forecast['ema50']:.2f}",
        f"[{ema_color}]{ema_cross}[/{ema_color}]"
    )
    tbl.add_row(
        "Bollinger Band",
        "—",
        "[dim]Price within bands[/dim]"
    )

    return Panel(tbl, title="[bold]⚙️  Technical Indicators[/bold]", border_style="magenta")


def make_sentiment_panel(articles: list[dict], agg: dict) -> Panel:
    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Sentiment", width=10)
    tbl.add_column("Score", width=7, justify="center")
    tbl.add_column("Headline", no_wrap=False)

    for a in articles[:8]:
        tbl.add_row(
            f"[{a['color']}]{a['icon']} {a['sentiment']}[/{a['color']}]",
            f"[{a['color']}]{a['compound']:+.2f}[/{a['color']}]",
            f"[dim]{a['title']}[/dim]",
        )

    overall = Text(justify="center")
    overall.append(
        f"\n  Overall: {agg['label']}  (score: {agg['score']:+.3f})  "
        f"▲{agg['bullish']} Bullish  ▼{agg['bearish']} Bearish  ●{agg['neutral']} Neutral\n",
        style=agg["color"]
    )

    from rich.console import Group
    content = Group(tbl, overall)

    return Panel(content, title="[bold]📰 News Sentiment[/bold]", border_style="yellow")


def make_forecast_panel(forecast: dict, sentiment: dict) -> Panel:
    signal     = forecast["signal"]
    sig_color  = forecast["sig_color"]
    bias       = forecast["bias_pct"]
    bias_color = "green" if bias >= 0 else "red"

    tbl = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True)
    tbl.add_column("Label", style="dim", width=24)
    tbl.add_column("Value", justify="right")

    tbl.add_row(
        "🎯 Next Session Signal",
        f"[{sig_color}]{signal}[/{sig_color}]"
    )
    tbl.add_row(
        "Forecast Price (mid)",
        f"[bold white]₹{forecast['mid']:,.2f}[/bold white]"
    )
    tbl.add_row(
        "Expected Range",
        f"[green]₹{forecast['low']:,.2f}[/green] – [red]₹{forecast['high']:,.2f}[/red]"
    )
    tbl.add_row(
        "Bias (%)",
        f"[{bias_color}]{bias:+.3f}%[/{bias_color}]"
    )
    tbl.add_row("", "")
    tbl.add_row(
        "Sentiment Contribution",
        f"[{'green' if sentiment['score']>=0 else 'red'}]{sentiment['score']:+.3f}[/]"
    )
    tbl.add_row(
        "Volume Conviction",
        f"[cyan]{forecast['conviction']:.2f}x[/cyan]"
    )
    tbl.add_row(
        "Swing Range Est.",
        f"₹{forecast['avg_range']:,.1f} avg daily move"
    )

    disclaimer = Text(
        "\n⚠  Forecast is indicative. Not financial advice. Always use stop-losses.\n",
        style="dim italic", justify="center"
    )

    from rich.console import Group
    content = Group(tbl, disclaimer)

    return Panel(content, title="[bold]🔮 Price Forecast (Next Session)[/bold]", border_style="green")


def build_dashboard(quote: dict, df: pd.DataFrame, articles: list[dict], agg: dict, forecast: dict) -> Layout:
    layout = Layout()
    last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    layout.split_column(
        Layout(name="header",     size=6),
        Layout(name="main"),
        Layout(name="sentiment",  size=16),
        Layout(name="forecast",   size=14),
    )

    layout["main"].split_row(
        Layout(name="price"),
        Layout(name="volume"),
        Layout(name="technicals"),
    )

    layout["header"].update(make_header(quote, last_updated))
    layout["price"].update(make_price_panel(quote, forecast))
    layout["volume"].update(make_volume_panel(quote, forecast))
    layout["technicals"].update(make_technicals_panel(forecast))
    layout["sentiment"].update(make_sentiment_panel(articles, agg))
    layout["forecast"].update(make_forecast_panel(forecast, agg))

    return layout


# ══════════════════════════════════════════════
# MAIN — Live refresh loop
# ══════════════════════════════════════════════

def check_api_key():
    if ALPHA_VANTAGE_KEY == "YOUR_API_KEY_HERE":
        console.print(Panel(
            "[bold red]⛔  Alpha Vantage API key not set![/bold red]\n\n"
            "1. Get a free key at: [link]https://www.alphavantage.co/support/#api-key[/link]\n"
            "2. Then run:\n"
            "   [bold cyan]export ALPHA_VANTAGE_KEY='your_key_here'[/bold cyan]\n"
            "   [bold cyan]python emcure_tracker.py[/bold cyan]\n\n"
            "   OR edit the script and set ALPHA_VANTAGE_KEY = 'your_key_here'",
            title="API Key Required",
            border_style="red"
        ))
        sys.exit(1)


def loading_panel(message: str) -> Panel:
    return Panel(
        Align.center(Text(f"\n⏳  {message}\n", style="bold yellow")),
        border_style="yellow"
    )


def main():
    check_api_key()

    console.print(f"\n[bold cyan]🚀 Starting {STOCK_NAME} Swing Trader Dashboard...[/bold cyan]\n")
    console.print(f"  Fetching data every [bold]{REFRESH_SECONDS}s[/bold]. Press [bold red]Ctrl+C[/bold red] to exit.\n")

    with Live(loading_panel("Connecting to Alpha Vantage & Google News..."),
              refresh_per_second=1, screen=True) as live:

        while True:
            try:
                # ── Fetch all data ──
                live.update(loading_panel("Fetching daily OHLCV data from Alpha Vantage..."))
                df = fetch_daily_data(STOCK_SYMBOL, ALPHA_VANTAGE_KEY)

                live.update(loading_panel("Fetching latest quote..."))
                quote = fetch_intraday_quote(STOCK_SYMBOL, ALPHA_VANTAGE_KEY)

                live.update(loading_panel("Fetching news from Google News RSS..."))
                articles = fetch_news(NEWS_RSS_URL, MAX_NEWS_ITEMS)

                # ── Fallback if data unavailable ──
                if df is None or df.empty:
                    live.update(Panel(
                        "[red]⚠  Could not fetch daily data from Alpha Vantage.\n"
                        "Check your API key and symbol (currently: [bold]" + STOCK_SYMBOL + "[/bold]).\n\n"
                        f"Retrying in {REFRESH_SECONDS}s...[/red]",
                        title="Data Error", border_style="red"
                    ))
                    time.sleep(REFRESH_SECONDS)
                    continue

                if quote is None:
                    # Use last row of daily data as fallback
                    last = df.iloc[-1]
                    quote = {
                        "price":      last["close"],
                        "open":       last["open"],
                        "high":       last["high"],
                        "low":        last["low"],
                        "prev_close": df.iloc[-2]["close"] if len(df) > 1 else last["close"],
                        "change":     last["close"] - (df.iloc[-2]["close"] if len(df) > 1 else last["close"]),
                        "change_pct": "N/A",
                        "volume":     last["volume"],
                        "latest_trading_day": str(last["date"].date()),
                    }

                # ── Compute ──
                agg      = aggregate_sentiment(articles)
                forecast = forecast_price(df, quote, agg)

                # ── Render ──
                dashboard = build_dashboard(quote, df, articles, agg, forecast)
                live.update(dashboard)

                # ── Sleep with countdown ──
                for remaining in range(REFRESH_SECONDS, 0, -1):
                    time.sleep(1)
                    # Update header countdown (lightweight — just re-render)
                    if remaining % 30 == 0:
                        dashboard = build_dashboard(quote, df, articles, agg, forecast)
                    live.update(dashboard)

            except KeyboardInterrupt:
                break
            except Exception as e:
                live.update(Panel(
                    f"[red]Error: {e}[/red]\n\nRetrying in {REFRESH_SECONDS}s...",
                    title="⚠  Error", border_style="red"
                ))
                time.sleep(REFRESH_SECONDS)

    console.print("\n[bold cyan]👋 Dashboard closed. Happy trading![/bold cyan]\n")


if __name__ == "__main__":
    main()
