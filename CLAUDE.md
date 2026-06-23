# EmcurePriceTracker — Implementation Instructions

## What This Project Does

A fully automated NSE intraday swing trading system for Emcure Pharmaceuticals (`EMCURE.NS`).  
It runs headlessly on **Oracle Cloud Free Tier (Mumbai)**, sends scheduled WhatsApp alerts via Twilio, and accepts trade commands directly from WhatsApp.

**No paid data APIs** — all market data from yfinance.

---

## Live Deployment

| Resource | Value |
|----------|-------|
| Server | Oracle Cloud VM — `<SERVER_IP>` (ap-mumbai-1, ARM A1.Flex) |
| Webhook | `https://<YOUR_DOMAIN>/whatsapp` |
| Health | `https://<YOUR_DOMAIN>/health` |
| SSH | `ssh -i emcurekey ubuntu@<SERVER_IP>` |

**Services running on server:**
- `emcure-bot` — WhatsApp Flask webhook (bot_server.py, port 5001)
- `emcure-tracker` — Headless alert engine (main_headless.py)

**Logs:**
```bash
tail -f /var/log/emcure/bot.log
tail -f /var/log/emcure/tracker.log
```

---

## Project Structure

```
EmcurePriceTracker/
├── main.py                  # Interactive dashboard (Rich UI) + alert engine
├── main_headless.py         # Headless mode for server deployment
├── bot_server.py            # Flask WhatsApp webhook (BUY/SELL/STATUS/HELP)
├── trade.py                 # CLI: python trade.py buy/sell/status
├── start_bot.sh             # Local dev: starts bot_server + ngrok tunnel
├── emcure_tracker.py        # Legacy entry point
├── src/
│   ├── data.py              # yfinance data fetching (daily + intraday)
│   ├── indicators.py        # RSI, MACD, Bollinger Bands, EMA, ATR, VWAP
│   ├── pivots.py            # Classic Pivot Points + Camarilla Pivots
│   ├── intraday.py          # SMA7 gap strategy, ORB, entry signals, rupee targets
│   ├── predictor.py         # Trade confidence predictor + WhatsApp message formatters
│   ├── sentiment.py         # FinBERT sentiment (VADER fallback) + Google News RSS
│   ├── scoring.py           # HMM market regime + combined signal scorer
│   ├── alerts.py            # Telegram + WhatsApp (Twilio) alert dispatcher
│   ├── dashboard.py         # Rich terminal UI panels
│   ├── trade_manager.py     # Manual trade state (T1/T2/T3/SL tracking)
│   └── news_monitor.py      # Background news polling thread
├── deploy/
│   ├── oracle_setup.sh      # Full Oracle Cloud deployment script (run once on server)
│   ├── bot.service          # systemd unit for bot_server.py
│   ├── nginx.conf           # nginx reverse proxy template
│   └── deploy.sh            # Legacy DigitalOcean deploy script
├── trade_state.json         # Runtime trade state — gitignored
├── strategy_state.json      # Supertrend strategy state — gitignored
├── requirements-core.txt    # Minimal deps for server (no torch/FinBERT)
├── requirements.txt         # Full deps including FinBERT
├── .env                     # Secrets — gitignored
└── .env.example
```

---

## Intraday Strategy

**Mean reversion from SMA7:**
- Entry condition: price ≥ ₹20 below 7-day SMA
- Strong entry: price ≥ ₹25 below SMA7
- Fixed rupee targets: T1 = +₹10, T2 = +₹20, T3 = +₹25
- SL = entry − (RISK_RUPEES / qty)

**Scheduled WhatsApp messages (auto, no trigger needed):**

| Time | Message |
|------|---------|
| 9:00–9:14 AM | Pre-open briefing — close, SMA7 gap, confidence score, entry zones |
| 9:20–9:59 AM | Post-open update — ORB, live price vs SMA7, trade plan |
| Intraday | Entry signal alert when gap ≤ −20 |
| T1/T2/T3/SL | Target hit alerts for active manual trades |
| 3:30–3:59 PM | EOD summary — OHLC, P&L, tomorrow's setup |

---

## WhatsApp Bot Commands

Send to **+14155238886** (Twilio sandbox):

| Command | Action |
|---------|--------|
| `BUY 1693` | Record entry at ₹1693, auto-compute qty from CAPITAL |
| `BUY 1693 60` | Record entry with explicit qty |
| `SELL` | Close trade, show final P&L |
| `STATUS` | Live P&L + level progress |
| `HELP` | Command list |

---

## Environment Variables (`.env`)

```
TICKER=EMCURE
REFRESH_SECONDS=300
CAPITAL=100000            # Trading capital in ₹
RISK_RUPEES=4500          # Max risk per trade in ₹
RISK_PCT=1.0              # Legacy — used by Supertrend strategy
MAX_DAILY_LOSS_PCT=3.0
FINBERT_MODEL_PATH=skip   # Set to 'skip' on server to avoid torch

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_WHATSAPP_FROM=+14155238886
TWILIO_WHATSAPP_TO=+91XXXXXXXXXX

TELEGRAM_TOKEN=           # Optional (periodically govt-blocked in India)
TELEGRAM_CHAT_ID=         # Optional

HEADLESS=true             # Set true on server

# ── Managed-cycle auto-trader (replaces Supertrend when enabled) ──
MANAGED_CYCLE=false       # true → run managed-cycle, DISABLE Supertrend for the symbol
MANAGED_CYCLE_LIVE=false  # false → dry-run (announces decisions, NO real orders)
MANAGED_TARGETS=15,20,30  # rupee targets from entry; sells at highest reachable today
MANAGED_SL=100            # stop = entry − ₹100
MANAGED_QTY=8             # re-entry position size (shares)
MANAGED_REENTRY_GAP=20    # re-enter when price ≥ ₹20 below the 7-day SMA
MANAGED_REACH_MIN_PROB=50 # aim for the highest target with reach-prob ≥ this %
                          # (dynamic, from live price + 7/14/30-day moves)
# Live-safety guards (Phase 2):
MANAGED_MAX_DAILY_LOSS=   # ₹ realized-loss cap/day → halts re-entries (default sl×qty)
MANAGED_REENTRY_COOLDOWN_MIN=60   # min minutes between an exit and the next entry
MANAGED_BLOCK_REENTRY_AFTER_STOP=true  # no re-entry the same day after a stop-out
```

**Alert channels (all additive — every alert fans out to each one configured):**
- **WhatsApp** (Twilio creds + `WHATSAPP_ENABLED=true`) — works in India; 50/day trial cap.
- **Telegram** (`TELEGRAM_TOKEN`+`TELEGRAM_CHAT_ID`) — server still sends, but blocked on the user's phone in India.

---

## Running Locally

```bash
# Full interactive dashboard
python main.py

# Headless (alerts only, no Rich UI)
HEADLESS=true python main.py

# CLI trade management
python trade.py buy 1693
python trade.py sell
python trade.py status

# WhatsApp bot (local dev with ngrok)
./start_bot.sh
```

---

## Deploying to Oracle Cloud

```bash
# SSH into server
ssh -i emcurekey ubuntu@<SERVER_IP>

# First-time setup (run once)
curl -fsSL https://raw.githubusercontent.com/heramb71/EmcurePriceTracker/main/deploy/oracle_setup.sh -o setup.sh
sudo bash setup.sh

# Update after code changes
cd /opt/emcure && sudo git pull
sudo systemctl restart emcure-bot emcure-tracker
```

**Key Oracle Cloud gotchas:**
- iptables REJECT rule is at position 5 — insert ACCEPT rules with `-I INPUT 5`, not `-A`
- VCN Security List must also have ports 80/443 open (two separate firewalls)
- Use `screen` or `tmux` for long-running SSH commands (pip install takes 5+ min)
- Run setup script as file (`sudo bash setup.sh`), not piped (`curl | sudo bash`) — stdin breaks `read` prompts

---

## NSE Trade Opportunity Radar (`src/radar/`, `radar_headless.py`, `radar.py`)

A **separate, read-only** multi-stock scanner — fully isolated from the live
EMCURE trading engine and the crypto service. It scans a 12-stock universe
(EMCURE, ICICIBANK, IREDA, IRFC, HUDCO, SUZLON + LAURUSLABS, RRKABEL, BHARATFORG,
APARINDS, KIRLOSENG, NETWEB), detects 5 signal types, scores
0–100, sends **Telegram alerts for manual review only**, and tracks every
signal's forward outcome to measure edge. **It never places trades.**

> Reality check: this exact universe failed the automation backtest
> (`swing_gate.py`: ~1.05 PF / ~0.7% CAGR; SMA7 reversion only generalizes to
> EMCURE+ICICIBANK). The radar is a *hypothesis validator*, not a recommender —
> alerts carry a mandatory "manual review / no auto-execution" footer and the
> success metric is forward expectancy, not alert count.

**Modules:**
- `universe.py` — 12 symbols + ADTV ≥ ₹100 Cr liquidity gate
- `features.py` — scalar per-stock snapshot (reuses `src/data.py` + `src/indicators.py`)
- `regime.py` — NIFTY regime: 50-DMA slope + ADX(14) + universe breadth → TRENDING_BULL/BEAR/SIDEWAYS
- `signals.py` — 5 detectors: SMA7 reversion, VWAP pullback, RVOL reversal, ATR breakout, gap reversion
- `scoring.py` — 0–100 confidence (RVOL/SMA7/VWAP/ATR/RSI/RS/regime), `SCORE_GATE=75`
- `scan.py` — pure pipeline → ranked, scored hits
- `dispatch.py` — cooldown + daily budget + digest batching (anti-flood)
- `alert_format.py` — the 🚨 TRADE OPPORTUNITY message + digest + `format_eod_stock` (per-stock EOD summary)
- `store.py` — SQLite (`radar.db`, gitignored): `signals` + `outcomes` tables
- `tracker.py` — evaluate matured outcomes at 1h/4h/1d/3d/5d/10d → MFE/MAE, WIN/LOSS/NEUTRAL
- `analytics.py` — win-rate / profit factor / expectancy by stock·signal·regime; leaders by expectancy

**Persistence:** one SQLite file (`radar.db`), stdlib `sqlite3`, WAL mode — no
server, OCI-free-tier friendly. The radar is the sole writer.

**Run:**
```bash
python radar.py scan-now      # one scan, ranked table (no alerts/writes)
python radar.py outcomes      # force a matured-outcome sweep
python radar.py report        # analytics dashboard
python radar_headless.py      # the service (market-aware loop)
```

**Deploy (separate service, leaves emcure-tracker/emcure-bot untouched):**
```bash
sudo cp /opt/emcure/deploy/radar.service /etc/systemd/system/emcure-radar.service
sudo systemctl daemon-reload && sudo systemctl enable --now emcure-radar
tail -f /var/log/emcure/radar.log
```

Config lives under the `RADAR_*` keys in `.env` (see `.env.example`). Telegram
only — reuses `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`.

**End-of-day summaries:** after market close on each trading day the radar sends
one per-stock EOD summary (OHLC, RSI/MACD/regime, tomorrow's SMA7 reversion watch
zone) in the EMCURE house style — `RADAR_EOD_SUMMARY=true` (default), excluding
`RADAR_EOD_EXCLUDE` (default `EMCURE`, which has its own managed EOD from
emcure-tracker). Watch zones are percentage-based (locked to the SMA7 signal's
1.4% threshold) so they scale across the ₹70–₹1800 price range; the summary is
watch-only and carries no tomorrow-probability claim (the reversion edge isn't
validated outside EMCURE).

---

## src/trade_manager.py

Manual trade state persistence for T1/T2/T3/SL alert monitoring.

State file: `trade_state.json` (gitignored)

```python
set_trade(entry: float, qty: int, risk_rupees: float) -> dict
clear_trade() -> None
get_trade() -> Optional[dict]
check_and_mark(price: float, day_high: float, day_low: float) -> list[dict]
current_pnl(price: float) -> Optional[dict]
format_target_alert(ticker: str, hit: dict, current_price: float) -> str
```

- `check_and_mark` uses `day_high` for T1/T2/T3, `day_low` for SL
- Each level fires alert only once (tracked in `levels_hit` list)

---

## src/predictor.py — Message Formatters

```python
format_pre_open_briefing(...)   # 9:00 AM briefing
format_post_open_briefing(...)  # 9:20 AM ORB update
format_eod_summary(...)         # 3:30 PM EOD close message
format_confidence_line(...)     # Single-line dashboard embed
```

---

## Coding Conventions

- Follow `src/` module boundaries — no cross-imports except via `main.py`
- All indicator functions are pure — take Series/DataFrame, return scalar or dict
- All network calls return `None`/empty on failure — never raise to caller
- Files: max 400 lines. Functions: max 50 lines
- No hardcoded prices or symbols outside `main.py` config block
- `trade_state.json` and `strategy_state.json` are runtime state — never commit
