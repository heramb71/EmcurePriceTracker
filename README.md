# EmcurePriceTracker

A terminal-based swing trading dashboard for NSE-listed stocks. Computes five technical signal systems, combines them into a single scored recommendation, and optionally sends Telegram/WhatsApp alerts when entry zones are detected.

**Default ticker:** Emcure Pharmaceuticals (`EMCURE.NS`). Change via `TICKER` env var.

---

## Quick Start

### 1. Clone & Install

```bash
git clone <repo-url>
cd EmcurePriceTracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-core.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env`:
```
TICKER=EMCURE
REFRESH_SECONDS=300
CAPITAL=100000
RISK_RUPEES=4500

# Optional: Telegram alerts
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional: WhatsApp alerts (Twilio)
TWILIO_ACCOUNT_SID=your_sid
TWILIO_AUTH_TOKEN=your_token
TWILIO_WHATSAPP_FROM=+14155238886
TWILIO_WHATSAPP_TO=+919876543210
```

### 3. Run

```bash
# Interactive terminal dashboard
python3 main.py

# Headless (no Rich UI — for servers or background use)
python3 main_headless.py

# Different ticker
TICKER=RELIANCE python3 main.py
```

Press `Ctrl+C` to exit.

---

## Architecture

### Module Structure

```
src/
├── data.py           # yfinance OHLCV fetching (daily + intraday + live quote)
├── indicators.py     # RSI, MACD, Bollinger Bands, EMA, ATR, VWAP
├── pivots.py         # Classic & Camarilla pivot levels
├── sentiment.py      # FinBERT sentiment (VADER fallback) + RSS news
├── scoring.py        # HMM regime detection + signal scoring + ML target probs
├── alerts.py         # Telegram & WhatsApp alert dispatch
├── dashboard.py      # Rich terminal UI panels & layout
├── predictor.py      # Backtest-driven confidence score + WhatsApp briefing
├── intraday.py       # SMA7 gap, ORB, entry signal, rupee targets
├── supertrend.py     # Supertrend indicator (period=10, multiplier=3.0)
├── strategy.py       # 3-layer Supertrend strategy: gate, sizing, management
├── state.py          # JSON state persistence (position, session, journal)
└── news_monitor.py   # Background 24h news sentiment thread

main.py               # Refresh loop, alert dispatch, state machine
main_headless.py      # Same as main.py but no Rich UI (for servers)
```

### Intraday Strategy (Mean Reversion)

1. **Entry signal** — price ≥ ₹20 below 7-day SMA triggers BUY; ≥ ₹25 triggers STRONG BUY
2. **Downtrend filter** — skip if 7D trend is Downward
3. **ORB confirmation** — Opening Range Breakout (first 15 min high/low) used to confirm entry
4. **Targets** — T1 = +₹10, T2 = +₹20 (primary), T3 = +₹25 (stretch); square off before close
5. **NPLP rule** — on drastic falls, hold 3–4 days and exit at no profit no loss

### Confidence Predictor (`src/predictor.py`)

Converts 22-month backtest patterns into a real-time score (0–100):

| Factor | Source |
|--------|--------|
| Gap depth below SMA7 | deeper gap = higher win rate |
| Month seasonality | May/Jun/Oct are strong; Jul/Nov/Feb are weak |
| Prior consecutive losses | after 1 loss, win rate drops 85% → 40% |
| ATR / daily range | narrow days can't reach ₹25 target |

### Combined Score (`src/scoring.py`)

| Signal | Weight |
|--------|--------|
| Pivot proximity | 0.25 |
| RSI zone | 0.20 |
| VWAP relationship | 0.20 |
| MACD histogram | 0.15 |
| Sentiment | 0.10 |
| Volume vs avg | 0.10 |

Weights adjust dynamically based on HMM-detected regime (Trending Up / Down / Ranging).

**Score → Signal:** ≥ 0.70 → Strong Buy · ≥ 0.55 → Buy · ≤ 0.30 → Strong Sell · ≤ 0.45 → Sell · else → Hold

---

## Alerts

### WhatsApp (Twilio)

- **9:00 AM** — Pre-open briefing: close, SMA7, entry zones, confidence score, probability bars, why factors
- **9:20 AM** — Post-open update: open price, ORB forming, action signal, trade plan
- **Intraday** — BUY / STRONG BUY alerts with trade plan (entry, SL, T1/T2/T3, EV)
- **Score-based** — Strong Buy / Strong Sell from combined scorer, max once per 30 min

WhatsApp uses proportional fonts — probability bars and trade plan tables are wrapped in ``` monospace blocks for correct alignment.

### Telegram

Strong Buy / Strong Sell alerts only.

---

## Deployment (Headless Server)

To run unattended on a Linux cloud VM (DigitalOcean $5/mo, AWS EC2 t3.micro, Fly.io, etc.):

### 1. Set up the server

```bash
cd /opt
sudo git clone <repo-url> emcure_price_tracker
cd emcure_price_tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-core.txt
cp .env.example .env
nano .env   # fill in credentials
```

### 2. Install the systemd service

```bash
sudo cp deploy/emcure_price_tracker.service /etc/systemd/system/
```

Edit the service file and replace `/path/to/EmcurePriceTracker` with the real path:

```ini
WorkingDirectory=/opt/emcure_price_tracker
ExecStart=/opt/emcure_price_tracker/venv/bin/python3 /opt/emcure_price_tracker/main_headless.py
EnvironmentFile=/opt/emcure_price_tracker/.env
StandardOutput=append:/var/log/emcure_price_tracker.log
StandardError=append:/var/log/emcure_price_tracker.err
```

### 3. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable emcure_price_tracker.service
sudo systemctl start emcure_price_tracker.service
```

### 4. Monitor

```bash
sudo systemctl status emcure_price_tracker.service
sudo journalctl -u emcure_price_tracker.service -f
tail -f /var/log/emcure_price_tracker.log
```

### 5. Update

```bash
cd /opt/emcure_price_tracker
git pull
source venv/bin/activate
pip install -r requirements-core.txt
sudo systemctl restart emcure_price_tracker.service
```

**Tip:** Disable FinBERT on low-memory VMs (< 1GB RAM): add `FINBERT_MODEL_PATH=skip` to `.env`.

---

## Testing

```bash
python3 -m pytest tests/ -v
python3 -m pytest tests/ -q    # quick
```

---

## Dependencies

| Package | Role | Required |
|---------|------|----------|
| `yfinance` | Market data | ✅ |
| `pandas` / `numpy` | Data handling | ✅ |
| `scikit-learn` | ML indicators | ✅ |
| `rich` | Terminal UI | ✅ |
| `python-dotenv` | `.env` config | ✅ |
| `hmmlearn` | Regime detection | Recommended (falls back to "Unknown") |
| `vaderSentiment` | Sentiment fallback | Recommended |
| `transformers` / `torch` | FinBERT sentiment | Optional (~2GB; slow first load) |
| `python-telegram-bot` | Telegram alerts | Optional |
| `twilio` | WhatsApp alerts | Optional |
| `pytest` | Tests | Dev only |

---

## Performance Notes

`main.py` sets these before any sklearn import to avoid segfaults on macOS Apple Silicon:

```python
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
```

---

## Security

- `.env` is gitignored — never commit secrets
- Use `.env.example` as the template
- Rotate Telegram token and Twilio credentials periodically
- On servers: use SSH keys, firewall to necessary ports only

---

## Troubleshooting

**`ModuleNotFoundError`** — `pip install -r requirements-core.txt`

**FinBERT won't load** — Falls back to VADER automatically. To force: `pip install transformers torch`

**Telegram alerts not sending** — Check `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`; ensure bot has permission in the chat

**WhatsApp fails with error 63016** — Twilio sandbox 24h window expired; recipient must re-send the join keyword to `+14155238886`

**Service won't start** — `sudo journalctl -u emcure_price_tracker.service -n 50`

**Low memory on server** — Add `FINBERT_MODEL_PATH=skip` to `.env`

**Dashboard encoding issues** — Ensure terminal supports UTF-8; `rich` falls back to ASCII automatically

---

## Development

See [CLAUDE.md](CLAUDE.md) for detailed module specifications and coding conventions.

```bash
# Run tests
python3 -m pytest tests/ -v

# Extend: add indicators → src/indicators.py
# Extend: update scoring weights → src/scoring.py
# Extend: add dashboard panels → src/dashboard.py
```

---

**Happy trading!** 📈
