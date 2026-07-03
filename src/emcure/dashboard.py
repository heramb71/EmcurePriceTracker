from __future__ import annotations

from typing import Optional

from rich import box
from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── Helpers ───────────────────────────────────────────────────────────────────

def _pct_price(base: float, pct: float) -> float:
    return round(base * (1 + pct / 100), 2)


def _nearest_level_name(price: float, pivots: dict, cam: dict, bb: dict) -> str:
    """Return the name of the closest named level to `price`."""
    all_levels = {
        **{k: v for k, v in pivots.items()},
        **{k: v for k, v in cam.items()},
        "BB Upper": bb.get("upper", 0),
        "BB Lower": bb.get("lower", 0),
    }
    best_name, best_dist = "", float("inf")
    for name, val in all_levels.items():
        if val <= 0:
            continue
        d = abs(price - val)
        if d < best_dist:
            best_dist, best_name = d, name
    pct_away = best_dist / price * 100 if price > 0 else 999
    return f"≈ {best_name}" if pct_away < 1.5 else ""


def _entry_range(price: float, pivots: dict, cam: dict) -> tuple[float, float, str, str]:
    """
    Return (low, high, low_label, high_label) for the buy zone.
    Low  = strongest support below price.
    High = next support above low (or current price).
    """
    named = {
        "S3": pivots.get("S3", 0), "S2": pivots.get("S2", 0),
        "S1": pivots.get("S1", 0), "L4": cam.get("L4", 0),
        "L3": cam.get("L3", 0),   "PP": pivots.get("PP", 0),
    }
    below = sorted(
        [(v, k) for k, v in named.items() if 0 < v < price],
        reverse=True,
    )

    if len(below) >= 2:
        high_val, high_lbl = below[0]
        low_val,  low_lbl  = below[1]
    elif len(below) == 1:
        high_val, high_lbl = below[0]
        low_val,  low_lbl  = round(high_val * 0.985, 2), "−1.5%"
    else:
        high_val, high_lbl = price, "current"
        low_val,  low_lbl  = _pct_price(price, -1.5), "−1.5%"

    return low_val, high_val, low_lbl, high_lbl


# ── Panel builders ────────────────────────────────────────────────────────────

def _make_header(
    ticker: str, quote: Optional[dict], signal: str, sig_color: str,
    score: float, last_updated: str, next_refresh_secs: int,
) -> Panel:
    if quote is None:
        return Panel(Align.center(Text("⚠  No market data", style="red")), box=box.DOUBLE_EDGE)

    arrow = "▲" if quote["change"] >= 0 else "▼"
    price_color = "green" if quote["change"] >= 0 else "red"

    t = Text(justify="center")
    t.append(f"\n  {ticker}.NS     ", style="bold white")
    t.append(f"₹{quote['price']:,.2f}  ", style=f"bold {price_color}")
    t.append(f"{arrow} {quote['change']:+.2f} ({quote['change_pct']:+.2f}%)  ", style=price_color)
    t.append("     ", style="white")
    t.append(f"  {signal}  ", style=f"bold {sig_color}")
    t.append(f"  score {score:.2f}  ", style="dim")
    t.append(f"\n  {last_updated}  ·  next refresh in {next_refresh_secs}s  ", style="dim italic")

    return Panel(Align.center(t), box=box.DOUBLE_EDGE, style="bold blue")


def _make_entry_panel(
    quote: Optional[dict], pivots: Optional[dict], cam: Optional[dict],
    indicators: Optional[dict],
) -> Panel:
    title = "[bold cyan]📍 Entry Zone[/bold cyan]"
    if quote is None or pivots is None:
        return Panel(Text("⚠ Data unavailable", style="red"), title=title, border_style="cyan")

    price = quote["price"]
    low, high, low_lbl, high_lbl = _entry_range(price, pivots, cam or {})

    rsi   = (indicators or {}).get("rsi", 50)
    vwap  = (indicators or {}).get("vwap", 0)
    vol_r = quote.get("volume", 0) / max((indicators or {}).get("avg_volume", 1), 1)

    # Build hints as (color, label) pairs — never use markup strings in Text.append()
    hints: list[tuple[str, str]] = []
    if rsi < 35:
        hints.append(("green",  "RSI oversold — strong entry"))
    elif rsi < 45:
        hints.append(("yellow", "RSI bearish — wait for bounce"))
    if price < vwap > 0:
        hints.append(("red",   "Below VWAP — weak intraday"))
    elif vwap > 0:
        hints.append(("green", "Above VWAP — intraday strength"))
    if vol_r < 0.5:
        hints.append(("red",   f"Low volume ({vol_r:.1f}x avg)"))
    elif vol_r > 1.5:
        hints.append(("green", f"High volume ({vol_r:.1f}x avg)"))

    zone = Text(justify="center")
    zone.append("\n  Buy between  ", style="dim")
    zone.append(f"₹{low:,.2f}", style="bold green")
    zone.append(f"  ({low_lbl})  ", style="dim")
    zone.append("  —  ", style="dim")
    zone.append(f"₹{high:,.2f}", style="bold green")
    zone.append(f"  ({high_lbl})\n", style="dim")

    hint_line = Text(justify="center")
    hint_line.append("\n")
    for i, (color, label) in enumerate(hints):
        if i:
            hint_line.append("   ·   ", style="dim")
        hint_line.append(label, style=color)
    hint_line.append("\n")

    return Panel(Group(Align.center(zone), Align.center(hint_line)),
                 title=title, border_style="cyan")


def _prob_cell(prob: Optional[int]) -> Text:
    if prob is None:
        return Text("—", style="dim", justify="center")
    if prob >= 60:
        color = "bold green"
    elif prob >= 40:
        color = "yellow"
    else:
        color = "red"
    return Text(f"{prob}%", style=color, justify="center")


def _stop_prob_cell(prob: Optional[int]) -> Text:
    """Color stop-hit probability inversely: low is good (green), high is bad (red)."""
    if prob is None:
        return Text("—", style="dim", justify="center")
    if prob <= 15:
        color = "bold green"
    elif prob <= 30:
        color = "yellow"
    else:
        color = "bold red"
    return Text(f"{prob}%", style=color, justify="center")


def _make_intraday_panel(
    price: float,
    cam: Optional[dict],
    intraday_probs: Optional[dict],
) -> Panel:
    title = "[bold yellow]⚡ Intraday Targets[/bold yellow]"
    cm    = cam or {}
    probs = intraday_probs or {}

    h3 = cm.get("H3", 0)
    h4 = cm.get("H4", 0)
    stop_hit: Optional[int] = probs.get("stop_hit", None)  # type: ignore[assignment]

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
                expand=True, padding=(0, 1))
    tbl.add_column("Target", style="dim",     width=7)
    tbl.add_column("Price",  justify="right",  width=11)
    tbl.add_column("Gain",   justify="right",  width=6,  style="green")
    tbl.add_column("Hit%",   justify="center", width=6)
    tbl.add_column("Level",  justify="left",   style="dim")

    cam_hints = {
        _pct_price(price, 0.5): "≈ H3" if h3 > 0 and abs(_pct_price(price, 0.5) - h3) / price < 0.015 else "",
        _pct_price(price, 1.0): "≈ H3" if h3 > 0 and abs(_pct_price(price, 1.0) - h3) / price < 0.015 else (
            "≈ H4" if h4 > 0 and abs(_pct_price(price, 1.0) - h4) / price < 0.015 else ""
        ),
        _pct_price(price, 1.5): "≈ H4" if h4 > 0 and abs(_pct_price(price, 1.5) - h4) / price < 0.015 else "",
    }

    for pct in (0.5, 1.0, 1.5):
        tp   = _pct_price(price, pct)
        gain = tp - price
        prob = probs.get(float(pct), None)
        hint = cam_hints.get(tp, "")
        tbl.add_row(
            f"+{pct}%",
            f"[bold green]₹{tp:,.2f}[/bold green]",
            f"+₹{gain:,.0f}",
            _prob_cell(prob),
            hint,
        )

    # Stop-loss row as separator
    sl_price = _pct_price(price, -0.5)
    tbl.add_row(
        Text("−0.5%", style="red"),
        Text(f"₹{sl_price:,.2f}", style="red"),
        Text(f"−₹{price - sl_price:,.0f}", style="red"),
        _stop_prob_cell(stop_hit),
        Text("SL fire rate", style="dim"),
    )

    note = Text("\n  Hit% = reach target | SL fire rate = stop hit first (same session)", style="dim italic")
    return Panel(Group(tbl, note), title=title, border_style="yellow")


def _make_swing_panel(
    price: float,
    pivots: Optional[dict],
    cam: Optional[dict],
    indicators: Optional[dict],
    target_probs: Optional[dict],
) -> Panel:
    title = "[bold green]🎯 Swing Targets[/bold green]"

    bb = {
        "upper": (indicators or {}).get("bb_upper", 0),
        "lower": (indicators or {}).get("bb_lower", 0),
    }
    pvt   = pivots or {}
    cm    = cam or {}
    probs = target_probs or {}

    horizons = {2: "3d", 5: "5d", 7: "10d", 10: "15d"}

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
                expand=True, padding=(0, 1))
    tbl.add_column("Target", style="dim",     width=7)
    tbl.add_column("Price",  justify="right",  width=12)
    tbl.add_column("Gain",   justify="right",  width=7,  style="green")
    tbl.add_column("Prob",   justify="center", width=8)
    tbl.add_column("Win",    justify="center", width=5,  style="dim")
    tbl.add_column("Near",   justify="left",   style="dim")

    for pct in (2, 5, 7, 10):
        tp   = _pct_price(price, pct)
        gain = tp - price
        near = _nearest_level_name(tp, pvt, cm, bb)
        prob = probs.get(float(pct), probs.get(pct, None))
        win  = horizons[pct]
        tbl.add_row(
            f"+{pct}%",
            f"[bold green]₹{tp:,.2f}[/bold green]",
            f"+₹{gain:,.0f}",
            _prob_cell(prob),
            win,
            near,
        )

    note = Text("\n  Prob = hit rate before −2% SL fires", style="dim italic")
    return Panel(Group(tbl, note), title=title, border_style="green")


def _make_stoploss_panel(price: float) -> Panel:
    title = "[bold red]🛑 Stop Loss[/bold red]"

    tbl = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold cyan",
                expand=True, padding=(0, 1))
    tbl.add_column("Level",  style="dim",    width=8)
    tbl.add_column("Price",  justify="right", width=12)
    tbl.add_column("Loss",   justify="right", width=8,  style="red")
    tbl.add_column("Use when", style="dim")

    tbl.add_row(
        "−1%", f"[red]₹{_pct_price(price, -1):,.2f}[/red]",
        f"−₹{price - _pct_price(price, -1):,.0f}",
        "Scalp",
    )
    tbl.add_row(
        "−2%", f"[bold red]₹{_pct_price(price, -2):,.2f}[/bold red]",
        f"−₹{price - _pct_price(price, -2):,.0f}",
        "Swing ✓",
    )

    # Risk:Reward pill row using -2% SL
    rr = Text(justify="center")
    rr.append("\n  R:R with −2% SL  →  ", style="dim")
    for pct in (2, 5, 7, 10):
        rr_val = pct / 2
        color  = "green" if rr_val >= 1 else "yellow"
        rr.append(f"+{pct}%=1:{rr_val:.0f}  ", style=color)

    return Panel(Group(tbl, rr), title=title, border_style="red")


def _make_context_bar(
    quote: Optional[dict], indicators: Optional[dict], sentiment: Optional[dict],
    score_result: Optional[dict],
) -> Panel:
    title = "[bold]📊 Context[/bold]"
    if indicators is None:
        return Panel(Text("⚠ Data unavailable", style="red"), title=title, border_style="dim")

    rsi       = indicators.get("rsi", 50)
    macd_hist = indicators.get("macd_hist", 0)
    ema20     = indicators.get("ema20", 0)
    ema50     = indicators.get("ema50", 0)
    bb_upper  = indicators.get("bb_upper", 0)
    bb_lower  = indicators.get("bb_lower", 0)
    avg_vol   = indicators.get("avg_volume", 1)
    volume    = (quote or {}).get("volume", 0)
    price     = (quote or {}).get("price", 0)
    vol_ratio = volume / avg_vol if avg_vol else 1.0
    regime    = (score_result or {}).get("regime", "Unknown")
    sent_lbl  = (sentiment or {}).get("label", "Neutral")
    sent_col  = (sentiment or {}).get("color", "yellow")

    # RSI
    if rsi <= 30:   rsi_c, rsi_l = "green",  f"RSI {rsi:.0f} Oversold"
    elif rsi >= 70: rsi_c, rsi_l = "red",    f"RSI {rsi:.0f} Overbought"
    elif rsi <= 45: rsi_c, rsi_l = "yellow", f"RSI {rsi:.0f} Bearish"
    else:           rsi_c, rsi_l = "white",  f"RSI {rsi:.0f} Neutral"

    # MACD
    macd_c = "green" if macd_hist > 0 else "red"
    macd_l = f"MACD {'▲ Positive' if macd_hist > 0 else '▼ Negative'}"

    # EMA trend
    ema_c = "green" if ema20 > ema50 else "red"
    ema_l = "EMA Uptrend" if ema20 > ema50 else "EMA Downtrend"

    # BB position
    if price >= bb_upper:  bb_c, bb_l = "red",    "BB Overbought"
    elif price <= bb_lower: bb_c, bb_l = "green",  "BB Oversold"
    else:                   bb_c, bb_l = "white",  "BB Mid-range"

    # Volume
    if vol_ratio >= 2.0:   vol_c, vol_l = "bold green", f"Vol {vol_ratio:.1f}x High"
    elif vol_ratio >= 1.0: vol_c, vol_l = "green",      f"Vol {vol_ratio:.1f}x Normal"
    else:                  vol_c, vol_l = "red",        f"Vol {vol_ratio:.1f}x Low"

    pills = [
        (rsi_c, rsi_l), (macd_c, macd_l), (ema_c, ema_l),
        (bb_c, bb_l), (vol_c, vol_l), (sent_col, sent_lbl), ("cyan", f"Regime: {regime}"),
    ]
    t = Text(justify="center")
    t.append("\n  ")
    for i, (color, label) in enumerate(pills):
        if i:
            t.append("  ·  ", style="dim")
        t.append(f" {label} ", style=color)
    t.append("\n")

    return Panel(Align.center(t), title=title, border_style="dim")


def _make_strategy_panel(
    buy_signal: Optional[dict],
    sizing: Optional[dict],
    state: Optional[dict],
    pnl_unrealised: float,
    halted_reason: str,
    capital: float,
) -> Panel:
    title = "[bold magenta]🚀 Supertrend Strategy[/bold magenta]"
    if buy_signal is None:
        return Panel(
            Text("⚠ Strategy unavailable", style="red"),
            title=title,
            border_style="magenta",
        )

    st = state or {}
    position = st.get("position")
    session = st.get("session", {})
    conds = buy_signal.get("conditions", {})
    details = buy_signal.get("details", {})

    def pill(name: str, ok: bool, detail: str) -> tuple[str, str]:
        icon = "✓" if ok else "✗"
        color = "green" if ok else "red"
        return color, f"{icon} {name} ({detail})"

    rsi_val = float(details.get("rsi", 0.0))
    vol_r = float(details.get("vol_ratio", 0.0))
    candle = details.get("candle", {})
    candle_lbl = "bull" if candle.get("is_bullish") else "bear"
    if candle.get("is_doji"):
        candle_lbl = "doji"
    px = float(details.get("price", 0.0))
    st_val = float(details.get("supertrend", 0.0))
    trend_detail = f"px {'>' if px > st_val else '<'} ST" if st_val > 0 else "—"

    pills = [
        pill("Trend", conds.get("trend", False), trend_detail),
        pill("RSI 55–75", conds.get("momentum", False), f"{rsi_val:.0f}"),
        pill("Vol>avg", conds.get("volume", False), f"{vol_r:.1f}x"),
        pill("Candle", conds.get("candle", False), candle_lbl),
    ]
    conds_text = Text(justify="center")
    conds_text.append("\n  ")
    for i, (color, label) in enumerate(pills):
        if i:
            conds_text.append("   ", style="dim")
        conds_text.append(f" {label} ", style=color)
    conds_text.append("\n")

    pos_line = Text(justify="center")
    if position:
        entry = float(position["entry"])
        sl = float(position["sl"])
        t1 = float(position["t1"])
        qty = int(position["qty_remaining"])
        booked = "partial booked, SL→BE" if position.get("partial_booked") else "full size"

        pos_line.append("\n  POSITION OPEN  ", style="bold green")
        pos_line.append(f"{qty} sh @ ₹{entry:,.2f}  ", style="white")
        pos_line.append(f"SL ₹{sl:,.2f}  T1 ₹{t1:,.2f}  ", style="dim")
        pos_line.append(f"({booked})\n", style="dim")

        pnl_color = "green" if pnl_unrealised >= 0 else "red"
        sign = "+" if pnl_unrealised >= 0 else ""
        pos_line.append("  Unrealised P&L: ", style="dim")
        pos_line.append(f"{sign}₹{pnl_unrealised:,.0f}\n", style=pnl_color)
    elif halted_reason:
        pos_line.append("\n  ⛔ HALTED  ", style="bold red")
        pos_line.append(f"({halted_reason})\n\n", style="dim red")
    elif buy_signal.get("triggered") and sizing:
        pos_line.append("\n  🚀 BUY GATE FIRED  ", style="bold green")
        pos_line.append(
            f"qty {sizing['qty']} | entry ₹{sizing['entry']:,.2f} | "
            f"SL ₹{sizing['sl']:,.2f} | T1 ₹{sizing['t1']:,.2f}\n\n",
            style="dim",
        )
    else:
        pos_line.append("\n  Position: ", style="dim")
        pos_line.append("FLAT  ", style="white")
        failed = [k for k, v in conds.items() if not v and k != "regime_ok"]
        if failed:
            pos_line.append(f"(waiting — {' + '.join(failed)} failed)\n\n", style="dim red")
        else:
            pos_line.append("(all conditions met — ready)\n\n", style="dim green")

    s_pnl = float(session.get("session_pnl", 0.0))
    losses = int(session.get("consecutive_losses", 0))
    pnl_color = "green" if s_pnl >= 0 else "red"
    sign = "+" if s_pnl >= 0 else ""

    session_text = Text(justify="center")
    session_text.append("  Session ", style="dim")
    session_text.append(f"{sign}₹{s_pnl:,.0f}  ", style=pnl_color)
    session_text.append(f"· consec losses {losses}  ", style="dim")
    if position and capital > 0:
        cap_pct = position["qty_remaining"] * position["entry"] / capital * 100
        session_text.append(f"· capital used {cap_pct:.1f}%  ", style="dim")
    regime = details.get("regime", "Unknown")
    regime_color = "green" if regime == "Trending Up" else (
        "red" if regime == "Trending Down" else "yellow"
    )
    session_text.append(f"· regime ", style="dim")
    session_text.append(regime, style=regime_color)

    return Panel(
        Group(conds_text, pos_line, Align.center(session_text)),
        title=title,
        border_style="magenta",
    )


# ── Main layout builder ───────────────────────────────────────────────────────

def build_dashboard(
    quote:             Optional[dict],
    pivots:            Optional[dict],
    cam:               Optional[dict],
    atr_lvls:          Optional[dict],
    indicators:        Optional[dict],
    sentiment:         Optional[dict],
    score_result:      Optional[dict],
    target_probs:      Optional[dict] = None,
    intraday_probs:    Optional[dict] = None,
    buy_signal:        Optional[dict] = None,
    sizing:            Optional[dict] = None,
    strategy_state:    Optional[dict] = None,
    pnl_unrealised:    float = 0.0,
    halted_reason:     str = "",
    capital:           float = 0.0,
    last_updated:      str = "",
    next_refresh_secs: int = 0,
    ticker:            str = "EMCURE",
) -> Layout:
    """
    Trading card layout:

    ┌──────────────────────── HEADER ────────────────────────────┐
    ├──────────────────────── ENTRY ZONE ────────────────────────┤
    ├── INTRADAY TARGETS ──┬── SWING TARGETS ──┬── STOP LOSS ───┤
    ├──────────────────────── CONTEXT ───────────────────────────┤
    """
    price     = (quote or {}).get("price", 0.0)
    signal    = (score_result or {}).get("signal", "—")
    sig_color = (score_result or {}).get("signal_color", "white")
    score     = (score_result or {}).get("score", 0.0)

    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=5),
        Layout(name="entry",    size=7),
        Layout(name="trade",    size=13),
        Layout(name="strategy", size=9),
        Layout(name="context",  size=5),
    )
    layout["trade"].split_row(
        Layout(name="intraday", ratio=2),
        Layout(name="swing",    ratio=3),
        Layout(name="stops",    ratio=2),
    )

    layout["header"].update(
        _make_header(ticker, quote, signal, sig_color, score, last_updated, next_refresh_secs)
    )
    layout["entry"].update(
        _make_entry_panel(quote, pivots, cam, indicators)
    )
    layout["intraday"].update(
        _make_intraday_panel(price, cam, intraday_probs)
    )
    layout["swing"].update(
        _make_swing_panel(price, pivots, cam, indicators, target_probs)
    )
    layout["stops"].update(
        _make_stoploss_panel(price)
    )
    layout["strategy"].update(
        _make_strategy_panel(
            buy_signal, sizing, strategy_state, pnl_unrealised, halted_reason, capital
        )
    )
    layout["context"].update(
        _make_context_bar(quote, indicators, sentiment, score_result)
    )

    return layout
