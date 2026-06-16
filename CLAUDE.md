# EmcurePriceTracker — Implementation Instructions

## What This Project Does

A fully automated NSE intraday swing trading system for Emcure Pharmaceuticals (`EMCURE.NS`).  
It runs headlessly on **Oracle Cloud Free Tier (Mumbai)**, sends scheduled WhatsApp alerts via Twilio, and accepts trade commands directly from WhatsApp.

**No paid data APIs** — all market data from yfinance.

---

## Live Deployment

| Resource | Value |
|----------|-------|
| Server | Oracle Cloud VM — `152.67.11.24` (ap-mumbai-1, ARM A1.Flex) |
| Webhook | `https://emcure-bot.duckdns.org/whatsapp` |
| Health | `https://emcure-bot.duckdns.org/health` |
| SSH | `ssh -i emcurekey ubuntu@152.67.11.24` |

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
│   ├── alerts.py            # ntfy + Telegram + WhatsApp (Twilio) alert dispatcher
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

TELEGRAM_TOKEN=           # Optional (blocked in India — server still sends)
TELEGRAM_CHAT_ID=         # Optional

NTFY_BASE_URL=http://127.0.0.1:2586   # Self-hosted ntfy — primary push channel
NTFY_TOPIC=               # Set to enable ntfy; leave blank to disable
NTFY_TOKEN=               # ntfy access token (tk_...)

HEADLESS=true             # Set true on server
```

**Alert channels (all additive — every alert fans out to each one configured):**
- **ntfy** (`NTFY_TOPIC` set) — self-hosted push on the Oracle VM, primary channel; works where Telegram is blocked. iPhone receives via ntfy's `upstream-base-url: https://ntfy.sh` APNs relay.
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
ssh -i emcurekey ubuntu@152.67.11.24

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
