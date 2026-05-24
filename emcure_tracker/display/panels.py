from __future__ import annotations

from typing import Optional

from rich import box
from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from emcure_tracker import config
from emcure_tracker.backtest import BacktestResult
from emcure_tracker.data.market import QuoteData
from emcure_tracker.data.nse import FIIDIIData
from emcure_tracker.forecast import ForecastResult
from emcure_tracker.indicators import IndicatorResult, VolumeSignal, rsi_signal
from emcure_tracker.sentiment import SentimentResult


def make_header(quote: Optional[QuoteData], last_updated: str) -> Panel:
    if quote is None:
        return Panel(
            Align.center(Text("\n  ⚠  Market data unavailable  \n", style="red")),
            style="bold red",
            box=box.DOUBLE_EDGE,
        )
    arrow = "▲" if quote.change >= 0 else "▼"
    color = "green" if quote.change >= 0 else "red"
    text = Text(justify="center")
    text.append(f"\n  {config.STOCK_NAME}  ({config.STOCK_SYMBOL})\n", style="bold white")
    text.append(f"  ₹{quote.price:,.2f}  ", style=f"bold {color}")
    text.append(f"  {arrow} {quote.change:+.2f}  ({quote.change_pct})  ", style=color)
    text.append(f"\n  Last updated: {last_updated}  ", style="dim italic")
    return Panel(Align.center(text), style="bold blue", box=box.DOUBLE_EDGE)


def make_price_panel(
    quote: Optional[QuoteData],
    indicators: Optional[IndicatorResult],
) -> Panel:
    if quote is None or indicators is None:
        return Panel("[red]⚠ Data unavailable[/red]", title="📊 Price & Bands", border_style="blue")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Metric", style="dim", width=22)
    tbl.add_column("Value", justify="right")

    tbl.add_row("Current Price", f"[bold white]₹{quote.price:,.2f}[/bold white]")
    tbl.add_row("Open", f"₹{quote.open:,.2f}")
    tbl.add_row("High", f"[green]₹{quote.high:,.2f}[/green]")
    tbl.add_row("Low", f"[red]₹{quote.low:,.2f}[/red]")
    tbl.add_row("Prev Close", f"₹{quote.prev_close:,.2f}")
    tbl.add_row("52w High", f"[green]₹{quote.week_52_high:,.2f}[/green]")
    tbl.add_row("52w Low", f"[red]₹{quote.week_52_low:,.2f}[/red]")
    tbl.add_row("", "")
    tbl.add_row("EMA 20", f"₹{indicators.ema_short:,.2f}")
    tbl.add_row("EMA 50", f"₹{indicators.ema_long:,.2f}")
    tbl.add_row("BB Upper", f"[red]₹{indicators.bb_upper:,.2f}[/red]")
    tbl.add_row("BB Middle", f"₹{indicators.bb_mid:,.2f}")
    tbl.add_row("BB Lower", f"[green]₹{indicators.bb_lower:,.2f}[/green]")

    if indicators.resistance_levels:
        tbl.add_row("Resistance", "  ".join(f"₹{r:,.0f}" for r in indicators.resistance_levels[-3:]))
    if indicators.support_levels:
        tbl.add_row("Support", "  ".join(f"₹{s:,.0f}" for s in indicators.support_levels[:3]))

    return Panel(tbl, title="[bold]📊 Price & Bands[/bold]", border_style="blue")


def make_volume_panel(
    quote: Optional[QuoteData],
    indicators: Optional[IndicatorResult],
) -> Panel:
    if quote is None or indicators is None:
        return Panel("[red]⚠ Data unavailable[/red]", title="📦 Volume", border_style="cyan")

    avg_vol = indicators.avg_volume
    ratio = quote.volume / avg_vol if avg_vol else 1.0
    bar_len = 30
    filled = min(bar_len, int(bar_len * min(ratio, 3.0) / 3.0))
    bar = "█" * filled + "░" * (bar_len - filled)
    bar_color = "green" if ratio >= 1.5 else ("yellow" if ratio >= 0.8 else "red")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Metric", style="dim", width=22)
    tbl.add_column("Value", justify="right")

    tbl.add_row("Today's Volume", f"[bold white]{quote.volume:,}[/bold white]")
    tbl.add_row("20-day Avg Vol", f"{avg_vol:,}")
    tbl.add_row("Vol vs Avg", f"[{bar_color}]{ratio:.2f}x[/{bar_color}]")
    tbl.add_row("Volume Bar", f"[{bar_color}]{bar}[/{bar_color}]")
    if quote.delivery_pct > 0:
        d_color = "green" if quote.delivery_pct >= config.ALERT_DELIVERY_PCT_SPIKE else "yellow"
        tbl.add_row("Delivery %", f"[{d_color}]{quote.delivery_pct:.1f}%[/{d_color}]")

    return Panel(tbl, title="[bold]📦 Volume Analysis[/bold]", border_style="cyan")


def make_technicals_panel(indicators: Optional[IndicatorResult]) -> Panel:
    if indicators is None:
        return Panel("[red]⚠ Data unavailable[/red]", title="⚙️ Technicals", border_style="magenta")

    rsi_label, rsi_color = rsi_signal(indicators.rsi)
    macd_color = "green" if indicators.macd_hist > 0 else "red"
    macd_arrow = "▲" if indicators.macd_hist > 0 else "▼"
    ema_bull = indicators.ema_short > indicators.ema_long
    ema_color = "green" if ema_bull else "red"
    ema_label = "EMA20 > EMA50 (Bullish)" if ema_bull else "EMA20 < EMA50 (Bearish)"

    rs = indicators.sector_relative_strength
    rs_text = f"{rs:.3f}" if rs is not None else "N/A"
    rs_color = "green" if (rs or 0) >= 1.0 else "red"

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Indicator", style="dim", width=18)
    tbl.add_column("Value", justify="right")
    tbl.add_column("Signal", justify="center")

    tbl.add_row("RSI (14)", str(indicators.rsi), f"[{rsi_color}]{rsi_label}[/{rsi_color}]")
    tbl.add_row(
        "MACD",
        f"{indicators.macd:.2f}",
        f"[{macd_color}]{macd_arrow} {indicators.macd:.2f}/{indicators.macd_signal:.2f}[/{macd_color}]",
    )
    tbl.add_row(
        "MACD Hist",
        f"{indicators.macd_hist:.2f}",
        f"[{macd_color}]{'Positive' if indicators.macd_hist > 0 else 'Negative'}[/{macd_color}]",
    )
    tbl.add_row("EMA Cross", f"{indicators.ema_short:.0f}/{indicators.ema_long:.0f}", f"[{ema_color}]{ema_label}[/{ema_color}]")
    tbl.add_row("Sector RS", rs_text, f"[{rs_color}]{'Leading' if (rs or 0) >= 1 else 'Lagging'}[/{rs_color}]")

    return Panel(tbl, title="[bold]⚙️  Technical Indicators[/bold]", border_style="magenta")


def make_fii_dii_panel(fii_dii: Optional[FIIDIIData]) -> Panel:
    if fii_dii is None or not fii_dii.available:
        return Panel("[dim]FII/DII data unavailable[/dim]", title="🏦 FII / DII", border_style="blue")

    fii_color = "green" if fii_dii.fii_net >= 0 else "red"
    dii_color = "green" if fii_dii.dii_net >= 0 else "red"
    fii_arrow = "▲" if fii_dii.fii_net >= 0 else "▼"
    dii_arrow = "▲" if fii_dii.dii_net >= 0 else "▼"

    tbl = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True)
    tbl.add_column("Label", style="dim", width=14)
    tbl.add_column("Value", justify="right")

    tbl.add_row("FII Net", f"[{fii_color}]{fii_arrow} ₹{fii_dii.fii_net:,.1f} Cr[/{fii_color}]")
    tbl.add_row("DII Net", f"[{dii_color}]{dii_arrow} ₹{fii_dii.dii_net:,.1f} Cr[/{dii_color}]")
    tbl.add_row("Date", f"[dim]{fii_dii.date}[/dim]")

    return Panel(tbl, title="[bold]🏦 FII / DII Flows[/bold]", border_style="blue")


def make_sentiment_panel(sentiment: Optional[SentimentResult]) -> Panel:
    if sentiment is None:
        return Panel("[dim]⏳ Loading sentiment…[/dim]", title="📰 News Sentiment", border_style="yellow")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan", expand=True)
    tbl.add_column("Sentiment", width=12)
    tbl.add_column("Score", width=7, justify="center")
    tbl.add_column("Source", width=14)
    tbl.add_column("Headline", no_wrap=False)

    for a in sentiment.articles[:8]:
        tbl.add_row(
            f"[{a.color}]{a.icon} {a.sentiment}[/{a.color}]",
            f"[{a.color}]{a.score:+.2f}[/{a.color}]",
            f"[dim]{a.source}[/dim]",
            f"[dim]{a.title}[/dim]",
        )

    overall = Text(justify="center")
    overall.append(
        f"\n  Overall: {sentiment.label}  (score: {sentiment.score:+.3f})  "
        f"▲{sentiment.bullish} Bullish  ▼{sentiment.bearish} Bearish  ●{sentiment.neutral} Neutral\n",
        style=sentiment.color,
    )
    return Panel(Group(tbl, overall), title="[bold]📰 News Sentiment[/bold]", border_style="yellow")


def make_forecast_panel(
    forecast: Optional[ForecastResult],
    sentiment: Optional[SentimentResult],
) -> Panel:
    if forecast is None:
        return Panel("[dim]⏳ Computing forecast…[/dim]", title="🔮 Forecast", border_style="green")

    bias_color = "green" if forecast.bias_pct >= 0 else "red"
    regime_color = {"trending": "green", "ranging": "yellow", "reverting": "cyan"}.get(forecast.regime, "dim")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True)
    tbl.add_column("Label", style="dim", width=24)
    tbl.add_column("Value", justify="right")

    tbl.add_row("🎯 Next Session Signal", f"[{forecast.sig_color}]{forecast.signal}[/{forecast.sig_color}]")
    tbl.add_row("Forecast Price (mid)", f"[bold white]₹{forecast.mid:,.2f}[/bold white]")
    tbl.add_row("Expected Range", f"[green]₹{forecast.low:,.2f}[/green] – [red]₹{forecast.high:,.2f}[/red]")
    tbl.add_row("Bias (%)", f"[{bias_color}]{forecast.bias_pct:+.3f}%[/{bias_color}]")
    tbl.add_row("Market Regime", f"[{regime_color}]{forecast.regime.capitalize()}[/{regime_color}]")
    tbl.add_row("", "")
    if sentiment:
        tbl.add_row("Sentiment", f"[{sentiment.color}]{sentiment.score:+.3f}[/{sentiment.color}]")
    tbl.add_row("Vol Conviction", f"[cyan]{forecast.conviction:.2f}x[/cyan]")
    tbl.add_row("Avg Daily Range", f"₹{forecast.avg_range:,.1f}")

    disclaimer = Text(
        "\n⚠  Indicative only. Not financial advice. Always use stop-losses.\n",
        style="dim italic",
        justify="center",
    )
    return Panel(Group(tbl, disclaimer), title="[bold]🔮 Price Forecast (Next Session)[/bold]", border_style="green")


def make_backtest_panel(bt) -> Panel:
    if bt is None:
        return Panel("[dim]⏳ Running backtest…[/dim]", title="📈 Backtest", border_style="dim")
    if not bt.available:
        return Panel("[dim]Backtest unavailable (install vectorbt)[/dim]", title="📈 Backtest", border_style="dim")

    win_color = "green" if bt.win_rate_pct >= 55 else ("yellow" if bt.win_rate_pct >= 45 else "red")
    dd_color = "green" if bt.max_drawdown_pct <= 10 else ("yellow" if bt.max_drawdown_pct <= 20 else "red")

    tbl = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True)
    tbl.add_column("Metric", style="dim", width=18)
    tbl.add_column("Value", justify="right")

    tbl.add_row("Win Rate", f"[{win_color}]{bt.win_rate_pct:.1f}%[/{win_color}]")
    tbl.add_row("Avg Return/Trade", f"{bt.avg_return_pct:+.2f}%")
    tbl.add_row("Max Drawdown", f"[{dd_color}]{bt.max_drawdown_pct:.1f}%[/{dd_color}]")
    tbl.add_row("Sharpe Ratio", f"{bt.sharpe:.2f}")
    tbl.add_row("Total Trades", str(bt.total_trades))
    tbl.add_row("Period", f"{bt.period_days} days")

    return Panel(tbl, title="[bold]📈 RSI Strategy Backtest[/bold]", border_style="dim")
