"""
Read-only web dashboard — an operator's-eye view of the live system.

Surfaces the Phase 1–2 work in one glance: is the tracker alive (heartbeat),
what's the open position and its P&L, and is the strategy actually making money
(ledger stats). Pure rendering: ``render_dashboard(ctx)`` turns a plain context
dict into a self-contained HTML string, so it unit-tests without a server, a
browser, or the network. The Flask route in apps/bot_server.py assembles ``ctx``
from the live sources and serves the result.

No client JS beyond a meta-refresh; inline CSS with design tokens so it reads as
an intentional trading-ops panel, not a stock template.
"""
from __future__ import annotations

import html
from typing import Any, Optional

_REFRESH_SECONDS = 30


def _pnl_class(value: Optional[float]) -> str:
    if value is None:
        return "flat"
    return "up" if value >= 0 else "down"


def _money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"₹{value:+,.0f}"


def _health_pill(age_seconds: Optional[float], market_open: bool) -> str:
    """Heartbeat freshness badge. Stale only matters while the market is open."""
    if age_seconds is None:
        state, label = ("down", "NO HEARTBEAT")
    elif market_open and age_seconds > 15 * 60:
        state, label = ("down", f"STALE {int(age_seconds // 60)}m")
    elif age_seconds > 60 * 60:
        state, label = ("idle", f"idle {int(age_seconds // 60)}m")
    else:
        state, label = ("live", f"live {int(age_seconds)}s ago")
    return f'<span class="pill {state}">{html.escape(label)}</span>'


def _position_card(pos: Optional[dict]) -> str:
    if not pos:
        return '<div class="card"><h2>Position</h2><p class="muted">No open position.</p></div>'
    pnl = pos.get("pnl")
    rows = [
        ("Source", html.escape(str(pos.get("source", "—")))),
        ("Entry", f"₹{pos.get('entry', 0):,.2f}"),
        ("Qty", str(pos.get("qty", 0))),
        ("Live", f"₹{pos.get('price', 0):,.2f}" if pos.get("price") else "—"),
        ("P&L", f'<span class="{_pnl_class(pnl)}">{_money(pnl)}</span>'),
    ]
    cells = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<div class="card"><h2>Position</h2><dl>{cells}</dl></div>'


def _stat_block(title: str, s: dict[str, Any]) -> str:
    if s["trades"] == 0:
        return (f'<div class="card"><h2>{html.escape(title)}</h2>'
                '<p class="muted">No closed trades yet.</p></div>')
    pf = "∞" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    rows = [
        ("Trades", f"{s['trades']}  ({s['wins']}W / {s['losses']}L)"),
        ("Win rate", f"{s['win_rate']:.0f}%"),
        ("Total P&L", f'<span class="{_pnl_class(s["total_pnl"])}">{_money(s["total_pnl"])}</span>'),
        ("Expectancy", f'<span class="{_pnl_class(s["expectancy"])}">{_money(s["expectancy"])}/trade</span>'),
        ("Profit factor", pf),
    ]
    cells = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<div class="card"><h2>{html.escape(title)}</h2><dl>{cells}</dl></div>'


def _recent_trades(trades: list[dict]) -> str:
    if not trades:
        return ""
    head = "<tr><th>Closed</th><th>Strat</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th></tr>"
    body = ""
    for t in trades:
        pnl = t.get("pnl")
        body += (
            "<tr>"
            f"<td>{html.escape(str(t.get('closed_at', '')))}</td>"
            f"<td>{html.escape(str(t.get('strategy', '')))}</td>"
            f"<td>{t.get('qty', 0)}</td>"
            f"<td>₹{t.get('entry_price', 0):,.2f}</td>"
            f"<td>₹{t.get('exit_price', 0):,.2f}</td>"
            f'<td class="{_pnl_class(pnl)}">{_money(pnl)}</td>'
            "</tr>"
        )
    return f'<div class="card wide"><h2>Recent trades</h2><table>{head}{body}</table></div>'


def render_dashboard(ctx: dict[str, Any]) -> str:
    """Render the full dashboard HTML from an assembled context dict."""
    ticker = html.escape(str(ctx.get("ticker", "EMCURE")))
    pill = _health_pill(ctx.get("heartbeat_age"), ctx.get("market_open", False))
    updated = html.escape(str(ctx.get("now", "")))

    per = ctx.get("by_strategy", {})
    strat_cards = "".join(_stat_block(name.capitalize(), block) for name, block in per.items())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">
<title>{ticker} · ops</title>
<style>
  :root {{
    --bg: oklch(18% 0.02 260); --surface: oklch(24% 0.02 260);
    --text: oklch(92% 0 0); --muted: oklch(64% 0.02 260);
    --up: oklch(78% 0.17 150); --down: oklch(68% 0.20 25);
    --line: oklch(32% 0.02 260); --accent: oklch(72% 0.15 260);
    --radius: 14px; --gap: clamp(0.75rem, 0.5rem + 1vw, 1.25rem);
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 ui-monospace, "SF Mono", Menlo, monospace; padding: var(--gap); }}
  header {{ display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
    margin-bottom: var(--gap); }}
  h1 {{ font-size: clamp(1.4rem, 1rem + 2vw, 2.2rem); margin: 0; letter-spacing: -0.02em; }}
  .updated {{ color: var(--muted); font-size: 0.8rem; margin-left: auto; }}
  .grid {{ display: grid; gap: var(--gap);
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
  .card {{ background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: var(--gap); }}
  .card.wide {{ grid-column: 1 / -1; }}
  h2 {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--muted); margin: 0 0 0.75rem; }}
  dl {{ display: grid; grid-template-columns: auto 1fr; gap: 0.35rem 1rem; margin: 0; }}
  dt {{ color: var(--muted); }}
  dd {{ margin: 0; text-align: right; font-variant-numeric: tabular-nums; }}
  .up {{ color: var(--up); }} .down {{ color: var(--down); }} .flat {{ color: var(--muted); }}
  .muted {{ color: var(--muted); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ text-align: right; color: var(--muted); font-weight: 500; padding: 0.3rem 0.5rem;
    border-bottom: 1px solid var(--line); }}
  th:first-child, td:first-child {{ text-align: left; }}
  td {{ padding: 0.3rem 0.5rem; border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums; }}
  .pill {{ padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.75rem;
    border: 1px solid var(--line); }}
  .pill.live {{ color: var(--up); border-color: var(--up); }}
  .pill.down {{ color: var(--down); border-color: var(--down); }}
  .pill.idle {{ color: var(--muted); }}
</style>
</head>
<body>
  <header>
    <h1>{ticker} <span class="muted">ops</span></h1>
    {pill}
    <span class="updated">updated {updated} · auto-refresh {_REFRESH_SECONDS}s</span>
  </header>
  <div class="grid">
    {_position_card(ctx.get("position"))}
    {_stat_block("All trades", ctx.get("summary", {"trades": 0}))}
    {strat_cards}
    {_recent_trades(ctx.get("recent_trades", []))}
  </div>
</body>
</html>"""
