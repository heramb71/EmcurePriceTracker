# Trading Workflow — EmcurePriceTracker

A trader/analyst-facing guide to how the system generates signals, manages trades,
controls risk, and (optionally) executes live on Zerodha. Kept in sync with the
code as of the CNC + confirm-fill rework.

> **TL;DR** — Two trend-following tools share one WhatsApp pipe:
> 1. **EMCURE equity swing trader** — Supertrend strategy, can auto-execute on Zerodha (CNC/delivery).
> 2. **BTC/ETH crypto monitor** — alerts only, no execution.

---

## 1. What it watches

| Instrument | Data | Frequency | Source |
|---|---|---|---|
| EMCURE.NS | Daily OHLCV + 5-min intraday | ~15-min delayed | yfinance |
| BTC-USD / ETH-USD | Daily + live quote, ₹-converted | live-ish | yfinance + USD/INR |
| Execution prices (auto-trade) | Live LTP & fills | real-time | Zerodha Kite |

**Key nuance for traders:** indicators are computed from daily history (Yahoo). When
auto-trading is active, the *current decision/management price* uses Zerodha's live LTP,
and order execution + recorded fill prices are live. yfinance has retry + a CoinGecko
fallback (crypto) so a transient data outage no longer blanks a cycle.

---

## 2. The two strategies

### A. Supertrend swing strategy — *this is what auto-trades*

**Entry gate** (all must hold simultaneously):

| Condition | Rule |
|---|---|
| Trend | Supertrend direction = UP (1) |
| Momentum | 40 < RSI < 75 |
| Volume | current volume ≥ 0.8 × average volume |
| Regime | **"Trending Up" — hard gate, blocks entries in Sideways/Down** |
| Event | not within 2 days of earnings (overnight gap guard) |

**Targets (ATR-scaled, off actual fill price):** T1 = +1×ATR, T2 = +2×ATR, T3 = +3×ATR
(≈ ₹35 / ₹70 / ₹105 at EMCURE's typical ATR ~₹35)
**Stop:** Entry − (1 × ATR), capped overall by `RISK_RUPEES`
**Product:** **CNC (delivery)** — holds overnight, no 3:20 PM auto-squareoff
**P&L:** reported **net of charges** (STT, exchange, GST, stamp); circuit breaker uses net

### B. SMA7 mean-reversion — *alerts only, you trade it manually*

- Buy zone when price ≥ ₹20 below the 7-day SMA; "strong" at ≥ ₹25 below.
- Same ₹10/₹20/₹25 targets. Drives the pre-open / post-open / EOD briefings.

### Confidence layer (sits on both)

- **HMM regime** — Trending Up / Sideways / Trending Down.
- **Sentiment** — FinBERT (VADER fallback) over Google News RSS.
- **ML probabilities** — logistic regression estimating the % chance of hitting each
  target, surfaced as the "% chance" lines in your messages.

---

## 3. What lands on your phone, and when (IST)

| Time | Message | What you act on |
|---|---|---|
| 9:00–9:14 AM | Pre-open briefing | Gap vs SMA7, confidence, entry zones, T1/T2/T3 odds |
| 9:00–9:14 AM | Auth heartbeat | ✅ active / 🚨 down — is auto-trading live today |
| 9:20–9:59 AM | Post-open / ORB | Live price vs SMA7, concrete ₹ trade plan |
| Intraday | Entry signal | Fires when the gate triggers |
| On hit | T1 / T2 / T3 / SL | Sell 1/3 at each target; SL = full exit |
| 3:30–3:59 PM | EOD summary | OHLC, P&L, tomorrow's setup |
| 8:00 AM / 8:00 PM | Crypto briefings | BTC/ETH trend, RSI, plain-English action |
| Crypto intraday | Signal alert | RSI < 35 or > 68 (4h cooldown per asset) |

---

## 4. Daily lifecycle (equity auto-trade)

```
Service running on Oracle Cloud (Mumbai)
    │
    ├─ 9:00–9:14  Pre-open: daily Kite re-auth + heartbeat + briefing
    │             Startup: reconcile bot state vs Zerodha holdings
    │
    ├─ 9:15–15:30 Market open — poll every REFRESH_SECONDS (default 300s)
    │       │
    │       ├─ Fetch data, compute indicators + Supertrend + regime
    │       ├─ Position open?
    │       │     ├─ Yes → manage exits (see §5)
    │       │     └─ No  → circuit breaker OK? → check entry gate → BUY
    │       └─ WhatsApp alert on every event
    │
    └─ 15:30      EOD summary → sleep until next session
```

Session counters (consecutive losses, P&L, halt flag) reset on a new trading day.

---

## 5. How a trade runs end-to-end (auto mode)

This is the **confirm-fill-then-record** flow (no more ghost positions):

```
Entry gate fires
    │
    ├─ Idempotency check: does Zerodha already hold untracked shares? → skip + alert
    ├─ Funds check: enough cash for full CNC cost? → else skip + alert
    │
    ├─ Place LIMIT order (LTP ± 0.1%) → POLL order_history until COMPLETE
    │     ├─ Filled    → record position at ACTUAL fill price; targets re-anchored
    │     └─ Not filled→ cancel order, open NO position, alert "BUY not placed"
    │
    └─ Manage position each tick (priority order):
          1. Price ≤ SL              → exit ALL (stop_hit)
          2. Supertrend flips down   → exit ALL (supertrend_exit)
          3. Price ≥ T3 (+3×ATR)     → sell 1/3
          4. Price ≥ T2 (+2×ATR)     → sell 1/3
          5. Price ≥ T1 (+1×ATR)     → sell 1/3, move stop to BREAKEVEN
       Every sell is fill-confirmed. A failed sell keeps you in
       position and sends 🚨 "exit manually".
```

After T1, the stop sits at entry — the trade is risk-free for the remaining shares.

---

## 6. Risk controls

| Control | Behaviour |
|---|---|
| Circuit breaker | Halt after **2 consecutive losses** or daily drawdown ≥ `MAX_DAILY_LOSS_PCT` |
| Position sizing | Risk-budgeted, **hard-capped by capital** (`qty ≤ capital // entry`) |
| Funds guard | Won't place a CNC order it can't fully fund |
| Idempotency | Confirms broker is flat before buying — no double positions |
| Reconciliation | Startup compares state vs Zerodha holdings; alerts on mismatch |
| Auth heartbeat | Daily ✅/🚨 so a silent login failure can't hide |
| Fill confirmation | State only changes on a confirmed fill, at the real fill price |

When halted: no new entries, but an open position is still managed to exit. Resets next day.

---

## 7. Auto vs manual — important

- **Only the Supertrend strategy places real orders.**
- WhatsApp **BUY/SELL commands are bookkeeping only** — you place the order in Zerodha
  yourself, then tell the bot so it tracks T1/T2/T3/SL.
- **Crypto is alerts-only** — no execution anywhere.

### Manual WhatsApp commands (bot_server.py → `trade_state.json`)

| Command | Effect |
|---|---|
| `BUY 1693` / `BUY 1693 60` | Record manual entry (auto-qty or explicit) |
| `SELL` | Close manual trade, show P&L |
| `STATUS` | Live P&L + level progress (auto-trade **and** manual) |
| `TOKEN <token>` | Complete Kite daily auth manually |
| `HELP` | Command list |

---

## 8. Configuration (`.env`)

```
KITE_AUTO_TRADE=true       # Master switch — must be "true" to place orders
KITE_API_KEY=              KITE_API_SECRET=
KITE_USER_ID=              KITE_PASSWORD=        KITE_TOTP_SECRET=

CAPITAL=15000              # Trading capital in ₹
RISK_RUPEES=600            # Max risk per trade in ₹
RISK_PCT=4.0               # % of capital risked per trade (sizing)
MAX_DAILY_LOSS_PCT=8.0     # Daily drawdown halt
REFRESH_SECONDS=300        # Poll interval
```

State files (all gitignored): `strategy_state.json` (auto-trade), `trade_state.json`
(manual), `/opt/emcure/kite_token.json` (daily Kite token).

---

## 9. Pros & Cons

### Pros
- **Disciplined & emotionless** — fixed rules, no fear/greed; staged profit-taking at 3 levels.
- **Risk-first** — breakeven stop after T1, circuit breaker, capital-capped sizing, funds guard.
- **Safe execution** — confirm-fill-then-record + reconciliation means the bot's books match reality, or it tells you they don't.
- **Zero recurring cost** — free data, free-tier hosting, WhatsApp via Twilio sandbox.
- **Always on** — runs headless 24/7; you get briefings even when not watching.
- **Strategy fits the instrument** — daily Supertrend + CNC suits EMCURE's slow, multi-day moves.
- **Transparent** — every action is a WhatsApp message and a log line; full trade journal.

### Cons
- **Single-name concentration** — all equity risk in one pharma stock; no diversification.
- **Overnight gap risk** — CNC holds overnight; a bad *unscheduled* headline can still gap past the stop (earnings are now guarded).
- **Indicator lag** — indicators use daily history; only the decision/management price is live (via Kite LTP when auto-trading).
- **Fragile daily auth** — depends on Zerodha's web login flow; breaks when they change it (heartbeat warns you, but trading is down until fixed).
- **Trend-only** — even with the regime gate, Supertrend can still whipsaw at regime transitions.
- **Slippage not modelled** — charges (STT/exchange/GST/stamp) are netted, but execution slippage isn't.

---

## 10. Improvements

### Shipped
- ✅ **ATR-scaled targets/stops** — T1/T2/T3 = +1/2/3 × ATR; stop = −1 × ATR.
- ✅ **Regime hard-gate** — entries blocked unless "Trending Up".
- ✅ **Event guard** — no new entries within 2 days of earnings.
- ✅ **Net P&L** — STT/exchange/GST/stamp netted; circuit breaker uses net.
- ✅ **Kite LTP for decisions** — live price drives signal/management when auto-trading.
- ✅ **Data fallback + retry** — yfinance retry + CoinGecko fallback (crypto).

### Remaining (by impact)
1. **Trailing stop after T2** — trail the last third (Supertrend/chandelier) to ride extended runs.
2. **Multi-symbol support** — generalise beyond EMCURE for diversification.
3. **Backtest the live ruleset** — validate ATR exits + regime gate + CNC + 1/3 staging as a unit before sizing up.
4. **Harden daily auth** — retry/backoff + clearer manual-token path; consider Kite's official login redirect.
5. **Equity data fallback** — add a non-yfinance source for EMCURE (crypto already has CoinGecko).
6. **Slippage modelling** — fold realistic slippage into journal P&L.
7. **Structured order journal** — append-only `order_id → status → fill → ts` log.
8. **Consolidate entry points** — one code path across `main.py` / `main_headless.py` / `emcure_tracker.py`.

**Portfolio / scale**
9. **Multi-symbol support** — generalise beyond EMCURE for diversification (config already mostly symbol-agnostic).
10. **Backtest the live ruleset** — the relaxed gate + CNC + 1/3 exits haven't been backtested as a unit; validate before sizing up.

**Ops**
11. **Structured order journal** — append-only `order_id → status → fill → ts` log for instant "did it trade?" answers.
12. **Consolidate entry points** — `main.py` / `main_headless.py` / `emcure_tracker.py` overlap; one code path reduces drift bugs.
```
