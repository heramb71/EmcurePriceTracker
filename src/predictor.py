"""
Real-time trade confidence predictor.

Converts backtest conditional probabilities into a pre-trade decision:
  - Confidence score (0–100)
  - Recommended target (T1/T2/T3 or SKIP)
  - Expected P&L
  - WhatsApp-ready briefing

Based on four empirical patterns from the EMCURE backtest
(22 months, 54 filtered trades):

  1. Gap depth     → deeper gap = higher win rate and larger pnl
  2. Month         → strong/weak seasonal bias
  3. Prior losses  → after 1 loss, win rate drops from 85% → 40%
  4. ATR/range     → narrow days never reach T3; wide days let you hold longer
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Lookup tables (derived from backtest)
# ─────────────────────────────────────────────────────────────────────────────

# gap_depth: (min_gap_abs, win_rate, t2plus_rate, t3_rate, avg_pnl)
_GAP_TABLE = [
    (55,  1.00, 1.00, 0.86, 1878),
    (35,  0.83, 0.67, 0.50, 1241),
    (25,  0.79, 0.50, 0.43,  553),
    (20,  0.73, 0.53, 0.47,  271),
]

# month bias: 1=Jan … 12=Dec → (win_rate, avg_pnl, label)
_MONTH_TABLE = {
    1:  (0.67, 877,   "Neutral"),
    2:  (0.50,   0,   "Weak"),
    3:  (1.00, 1541,  "Strong"),
    4:  (1.00, 1547,  "Strong"),
    5:  (1.00, 2158,  "Strong"),
    6:  (1.00, 1925,  "Strong"),
    7:  (0.33, -1067, "Weak"),
    8:  (0.80, -133,  "Neutral"),
    9:  (0.67, 503,   "Neutral"),
    10: (0.88, 1148,  "Strong"),
    11: (0.50, -187,  "Weak"),
    12: (1.00, 1320,  "Strong"),
}

# prior_losses: number of consecutive prior losses → (win_rate, note)
_LOSS_TABLE = {
    0: (0.85, "Fresh start"),
    1: (0.40, "⚠️ Elevated risk after 1 loss"),
    2: (0.67, "Recovery zone"),
}

# atr/range → (win_rate, t3_rate, target_rec)
_ATR_TABLE = [
    (80,  1.00, 0.75, "T3"),
    (50,  0.71, 0.59, "T2-T3"),
    (30,  0.80, 0.65, "T2-T3"),
    (0,   0.78, 0.00, "T1-T2"),  # narrow day: T3 never hit
]

# ─────────────────────────────────────────────────────────────────────────────
# Lookup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _gap_row(gap_abs: float) -> dict:
    for min_abs, wr, t2r, t3r, avg in _GAP_TABLE:
        if gap_abs >= min_abs:
            return {"win_rate": wr, "t2plus_rate": t2r, "t3_rate": t3r, "avg_pnl": avg}
    return {"win_rate": 0.73, "t2plus_rate": 0.53, "t3_rate": 0.47, "avg_pnl": 271}


def _atr_row(atr: float) -> dict:
    for min_atr, wr, t3r, target in _ATR_TABLE:
        if atr >= min_atr:
            return {"win_rate": wr, "t3_rate": t3r, "target_rec": target}
    return {"win_rate": 0.78, "t3_rate": 0.00, "target_rec": "T1-T2"}


# ─────────────────────────────────────────────────────────────────────────────
# Core predictor
# ─────────────────────────────────────────────────────────────────────────────

def predict_trade(
    gap: float,                    # price - SMA7 (negative = below)
    atr: float,                    # ATR(14) from daily data
    prior_losses: int = 0,         # consecutive losses before this trade
    qty: int = 0,                  # shares for EV calculation
    risk_per_trade: float = 4500,  # max rupee loss
    now: Optional[datetime] = None,
) -> dict:
    """
    Compute a trade confidence score and recommendation from first principles.

    Returns a dict with:
        score          0–100 confidence
        tier           A (≥70) / B (50–70) / C (<50) / SKIP
        target_rec     T1 / T1-T2 / T2-T3 / T3
        win_prob       estimated probability of profit
        ev             expected value in rupees
        factors        list of (label, impact) explaining the score
        month_label    Strong / Neutral / Weak
        prior_loss_note  warning if prior losses are elevated
    """
    if now is None:
        now = datetime.now()

    gap_abs = abs(gap)
    month   = now.month

    # ── Per-factor lookup ─────────────────────────────────────────────────────
    gap_data   = _gap_row(gap_abs)
    month_data = _MONTH_TABLE.get(month, (0.75, 500, "Neutral"))
    loss_data  = _LOSS_TABLE.get(min(prior_losses, 2), (0.67, ""))
    atr_data   = _atr_row(atr)

    month_wr, month_avg_pnl, month_label = month_data
    loss_wr, loss_note = loss_data

    # ── Bayesian-style weight combination ─────────────────────────────────────
    # Base win probability from gap depth (most predictive factor)
    base_wr = gap_data["win_rate"]

    # Multiplicative adjustment for season and recent losses
    # Season: scale between 0.6 (weak) and 1.1 (strong)
    season_factor = {
        "Strong":  1.10,
        "Neutral": 1.00,
        "Weak":    0.65,
    }.get(month_label, 1.0)

    # Loss factor: after 1 loss, shrink probability toward 40%
    loss_factor = {0: 1.0, 1: 0.55, 2: 0.85}.get(min(prior_losses, 2), 1.0)

    # ATR factor: very narrow days are harder (T3 unreachable)
    atr_factor = 1.05 if atr >= 50 else (0.90 if atr < 30 else 1.0)

    combined_wr = min(0.98, base_wr * season_factor * loss_factor * atr_factor)

    # ── Target recommendation ─────────────────────────────────────────────────
    t3_ok  = combined_wr >= 0.75 and atr_data["t3_rate"] > 0.40
    t2_ok  = combined_wr >= 0.60

    if   t3_ok and month_label == "Strong":
        target_rec = "T3 (₹25)"
    elif t3_ok:
        target_rec = "T2–T3 (₹20–25)"
    elif t2_ok:
        target_rec = "T1–T2 (₹10–20)"
    else:
        target_rec = "T1 (₹10) only"

    # ── Reach probabilities (cumulative — "will stock get to this level?") ───
    # These are what a trader actually wants to know.
    # reach_t3: probability stock touches entry + ₹25
    # reach_t2: probability stock touches entry + ₹20  (always >= reach_t3)
    # reach_t1: probability stock touches entry + ₹10  (= overall win rate)
    scale     = combined_wr / max(base_wr, 0.01)
    reach_t3  = min(0.95, gap_data["t3_rate"]      * scale)
    reach_t2  = min(0.95, gap_data["t2plus_rate"]  * scale)   # t2 OR t3
    reach_t1  = combined_wr                                    # any win = T1 reached
    p_stop    = max(0.01, 1.0 - combined_wr)

    # ── Exit distribution (mutually exclusive — for EV calculation only) ────
    # exit_t3: closes at T3 (reached ₹25)
    # exit_t2: reached ₹20 but reversed before ₹25
    # exit_t1: reached ₹10 but reversed before ₹20
    exit_t3 = reach_t3
    exit_t2 = max(0.0, reach_t2 - reach_t3)
    exit_t1 = max(0.0, combined_wr - reach_t2)

    if qty > 0:
        ev = round(
            exit_t3 * (25 * qty)
            + exit_t2 * (20 * qty)
            + exit_t1 * (10 * qty)
            - p_stop  * risk_per_trade,
            0,
        )
    else:
        ev = 0

    # ── Confidence score 0–100 ────────────────────────────────────────────────
    score = int(combined_wr * 100)

    tier = (
        "A — HIGH"     if score >= 75 else
        "B — MODERATE" if score >= 55 else
        "C — LOW"      if score >= 40 else
        "SKIP"
    )

    # ── Factor breakdown ──────────────────────────────────────────────────────
    factors = [
        (f"Gap depth −₹{gap_abs:.0f}",
         f"base win {base_wr*100:.0f}%  avg P&L ₹{gap_data['avg_pnl']:+,.0f}"),
        (f"Month: {now.strftime('%B')} ({month_label})",
         f"seasonal win {month_wr*100:.0f}%  avg ₹{month_avg_pnl:+,.0f}"),
        (f"Prior losses: {prior_losses}",
         loss_note + f"  factor ×{loss_factor:.2f}"),
        (f"ATR/Range: ₹{atr:.0f}",
         f"target reachable: {atr_data['target_rec']}"),
    ]

    return {
        "score":             score,
        "tier":              tier,
        "combined_win_prob": round(combined_wr * 100, 1),
        "target_rec":        target_rec,
        "ev":                ev,
        # Cumulative reach probabilities — shown to user
        "reach_t3":          round(reach_t3 * 100, 1),
        "reach_t2":          round(reach_t2 * 100, 1),
        "reach_t1":          round(reach_t1 * 100, 1),
        "p_stop":            round(p_stop  * 100, 1),
        # Exit distribution — kept for EV calc / internal use
        "exit_t3":           round(exit_t3 * 100, 1),
        "exit_t2":           round(exit_t2 * 100, 1),
        "exit_t1":           round(exit_t1 * 100, 1),
        "month_label":       month_label,
        "prior_loss_note":   loss_note,
        "factors":           factors,
        "gap_abs":           gap_abs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp message builders
# ─────────────────────────────────────────────────────────────────────────────

_TIER        = {"A — HIGH": "🟢", "B — MODERATE": "🟡", "C — LOW": "🟠", "SKIP": "🔴"}
_TREND_EMOJI = {"Upward": "📈", "Downward": "📉", "Choppy": "〰️"}


def _pbar(pct: float, width: int = 18) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def format_pre_open_briefing(
    ticker: str,
    price: float,
    sma7: float,
    trend_7d: str,
    atr: float,
    capital: float,
    risk_rupees: float,
    prior_losses: int = 0,
    sentiment_label: str = "Neutral",
    sentiment_score: float = 0.0,
    now: Optional[datetime] = None,
) -> str:
    """9:00 AM pre-open WhatsApp briefing."""
    if now is None:
        now = datetime.now()

    gap         = price - sma7
    qty         = int(capital / price) if price > 0 else 0
    pred        = predict_trade(gap, atr, prior_losses, qty, risk_rupees, now)
    tier_emoji  = _TIER.get(pred["tier"], "⚪")
    trend_emoji = _TREND_EMOJI.get(trend_7d, "📊")
    buy_zone    = round(sma7 - 20, 2)
    strong_zone = round(sma7 - 25, 2)
    sent_emoji  = "🟢" if sentiment_score > 0.05 else ("🔴" if sentiment_score < -0.05 else "🟡")

    # Monospace block for perfectly aligned probability bars
    bars = (
        "```\n"
        f"Confidence  {'▓' * round(pred['score']/5)}{'░' * (20 - round(pred['score']/5))} {pred['score']}%\n"
        "\n"
        f"T1 +₹10  {_pbar(pred['reach_t1'])} {pred['reach_t1']:.0f}%\n"
        f"T2 +₹20  {_pbar(pred['reach_t2'])} {pred['reach_t2']:.0f}%\n"
        f"T3 +₹25  {_pbar(pred['reach_t3'])} {pred['reach_t3']:.0f}%\n"
        f"Stop     {_pbar(pred['p_stop'])} {pred['p_stop']:.0f}%\n"
        "```"
    )

    confidence_label = (
        "High — good setup today 👍" if pred["score"] >= 75 else
        "Medium — decent chance"     if pred["score"] >= 55 else
        "Low — be cautious"          if pred["score"] >= 40 else
        "Very low — better to skip today"
    )

    lines = [
        f"🌅 *Good morning! {ticker} Pre-Market Update*",
        f"📅 {now.strftime('%a, %d %b %Y')}",
        "",
        f"Yesterday closed at ₹{price:,.2f}",
        f"7-day average: ₹{sma7:,.2f}",
        f"Stock is ₹{abs(gap):.0f} {'below' if gap < 0 else 'above'} its average",
        f"Trend this week: {trend_emoji} {trend_7d}",
        f"News mood: {sent_emoji} {sentiment_label}",
        "",
        f"🎯 *When to buy today:*",
        f"Good entry if price drops to ₹{buy_zone:,.2f} or below",
        f"Strong entry if price drops to ₹{strong_zone:,.2f} or below",
        "",
        f"{tier_emoji} *Today's confidence: {confidence_label}*",
        f"Chance of reaching +₹10: {pred['reach_t1']:.0f}%",
        f"Chance of reaching +₹20: {pred['reach_t2']:.0f}%",
        f"Chance of reaching +₹25: {pred['reach_t3']:.0f}%",
        f"Chance of stop loss hit: {pred['p_stop']:.0f}%",
    ]

    if qty > 0 and pred["ev"] != 0:
        ev_label = "expected profit" if pred["ev"] > 0 else "expected loss"
        lines.append(f"Expected outcome per trade: ₹{pred['ev']:+,.0f} ({ev_label})")

    if pred["prior_loss_note"] and prior_losses > 0:
        lines += ["", f"⚠️ {pred['prior_loss_note']}"]

    lines.append(f"\n⏰ Market opens at 9:15 AM")
    return "\n".join(lines)


def format_post_open_briefing(
    ticker: str,
    open_price: float,
    current_price: float,
    sma7: float,
    orb: dict,
    atr: float,
    capital: float,
    risk_rupees: float,
    prior_losses: int = 0,
    now: Optional[datetime] = None,
) -> str:
    """9:20 AM post-open WhatsApp message (ORB forming)."""
    if now is None:
        now = datetime.now()

    gap  = current_price - sma7
    qty  = int(capital / current_price) if current_price > 0 else 0
    pred = predict_trade(gap, atr, prior_losses, qty, risk_rupees, now)

    signal_emoji = (
        "🔔🔔" if pred["score"] >= 75 and gap <= -20 else
        "🔔"  if pred["score"] >= 55 and gap <= -20 else
        "👀"
    )
    tier_emoji = _TIER.get(pred["tier"], "⚪")

    orb_str = (
        f"₹{orb['low']:,.2f} – ₹{orb['high']:,.2f}  (range ₹{orb['range']:.0f})"
        if orb.get("valid") else "forming…"
    )

    below_orb = orb.get("valid") and current_price < orb.get("low", 0)
    in_buy    = gap <= -20

    action = (
        "STRONG BUY" if (below_orb and gap <= -20) or gap <= -25 else
        "BUY"        if in_buy else
        "WATCH"      if gap > -20 and gap > -30 else
        "WAIT"
    )

    lines = [
        f"{signal_emoji} *{ticker} — Market Open Update*",
        f"⏰ 9:20 AM",
        "",
        f"Opened at ₹{open_price:,.2f}",
        f"Currently at ₹{current_price:,.2f}",
        f"7-day average: ₹{sma7:,.2f}  (₹{abs(gap):.0f} {'below' if gap < 0 else 'above'})",
        f"Early range: {orb_str}",
        "",
        f"🚦 *Signal: {action}*",
    ]

    if action in ("BUY", "STRONG BUY") and qty > 0:
        entry = current_price
        sl    = round(entry - risk_rupees / qty, 2)
        lines += [
            "",
            f"📋 *If buying now ({qty} shares):*",
            f"Buy at:       ₹{entry:,.2f}",
            f"Sell half at: ₹{round(entry + 10):,.2f}  (profit ₹{10 * qty:,.0f})",
            f"Final target: ₹{round(entry + 20):,.2f}  (profit ₹{20 * qty:,.0f})",
            f"Stop loss:    ₹{sl:,.2f}  (max loss ₹{risk_rupees:,.0f})",
        ]
    else:
        lines.append(f"Stock is not in buy zone yet. Keep watching.")

    return "\n".join(lines)


def format_confidence_line(pred: dict) -> str:
    """Single-line summary for dashboard embedding."""
    tier_emoji = _TIER.get(pred["tier"], "⚪")
    return (
        f"{tier_emoji} Confidence {pred['score']}/100  "
        f"win {pred['combined_win_prob']:.0f}%  "
        f"target {pred['target_rec']}  "
        f"EV ₹{pred['ev']:+,.0f}"
    )


def format_eod_summary(
    ticker: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
    change_pct: float,
    sma7: float,
    atr: float,
    capital: float,
    risk_rupees: float,
    prior_losses: int = 0,
    day_pnl: float = 0.0,
    trades_today: int = 0,
    now: Optional[datetime] = None,
) -> str:
    """3:30 PM end-of-day WhatsApp summary with tomorrow's setup preview."""
    if now is None:
        now = datetime.now()

    gap          = close - sma7
    change_emoji = "🟢" if change_pct >= 0 else "🔴"
    candle_emoji = "🕯️"
    if close > open_price and (high - low) > 0:
        body_pct = (close - open_price) / (high - low)
        candle_emoji = "🟢" if body_pct > 0.5 else "📊"
    elif close < open_price:
        candle_emoji = "🔴"

    # Tomorrow's entry zones based on today's close and SMA7
    buy_zone    = round(sma7 - 20, 2)
    strong_zone = round(sma7 - 25, 2)

    # Quick tomorrow prediction using today's close vs SMA7
    qty  = int(capital / close) if close > 0 else 0
    pred = predict_trade(gap, atr, prior_losses, qty, risk_rupees, now)
    tier_emoji = _TIER.get(pred["tier"], "⚪")

    setup_signal = (
        "🔔 *SETUP FORMING*" if gap <= -15 else
        "👀 *WATCH ZONE*"    if gap <= -10 else
        "⏳ *TOO FAR — Wait*" if gap > 0   else
        "📊 *Near SMA7*"
    )

    lines = [
        f"🌆 *{ticker} — End of Day Summary*",
        f"📅 {now.strftime('%a, %d %b %Y')}",
        "",
        f"Opened:  ₹{open_price:,.2f}",
        f"Highest: ₹{high:,.2f}",
        f"Lowest:  ₹{low:,.2f}",
        f"Closed:  ₹{close:,.2f}  {change_emoji} {change_pct:+.2f}%",
    ]

    if trades_today > 0:
        pnl_emoji = "✅" if day_pnl >= 0 else "❌"
        lines += [
            "",
            f"{pnl_emoji} *Today's trading P&L: ₹{day_pnl:+,.0f}*",
        ]

    tomorrow_confidence = (
        "High — looks like a good setup 👍" if pred["score"] >= 75 else
        "Medium — worth watching"           if pred["score"] >= 55 else
        "Low — may not trigger tomorrow"
    )

    lines += [
        "",
        f"── *Tomorrow's Outlook* ──",
        f"{setup_signal}",
    ]

    if gap <= -15:
        lines += [
            f"Stock is ₹{abs(gap):.0f} below its average — getting into buy territory.",
            f"Watch for a buy if price stays below ₹{buy_zone:,.2f}",
        ]
    elif gap > 0:
        lines += [
            f"Stock is above its average — no buy setup yet.",
            f"Wait for a pullback to ₹{buy_zone:,.2f} or lower.",
        ]
    else:
        lines.append(f"Stock is near its average. Buy zone: ₹{buy_zone:,.2f}")

    lines += [
        "",
        f"{tier_emoji} Confidence for tomorrow: {tomorrow_confidence}",
        f"Chance of +₹10: {pred['reach_t1']:.0f}%  |  +₹20: {pred['reach_t2']:.0f}%  |  +₹25: {pred['reach_t3']:.0f}%",
        "",
        f"⏰ Next update: tomorrow at 9:00 AM",
    ]

    return "\n".join(lines)
