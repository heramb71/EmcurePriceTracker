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
├── apps/                     # Entry points — run as modules: python -m apps.<name>
│   ├── main.py               # Interactive dashboard (Rich UI) + alert engine
│   ├── main_headless.py      # Headless EMCURE service for server deployment
│   ├── bot_server.py         # Flask WhatsApp webhook + Telegram command bot
│   ├── trade.py              # CLI: python -m apps.trade buy/sell/status
│   ├── crypto_headless.py    # Crypto BTC/ETH tracker service
│   ├── radar_headless.py     # Multi-stock radar scanner service
│   ├── radar.py              # Radar CLI (scan-now / outcomes / report)
│   ├── emcure_tracker.py     # Legacy entry point
│   └── *_backtest.py, reversion_lab.py, swing_gate.py, send_whatsapp_now.py  # research/CLI tools
├── src/                      # Library code — organized by feature/domain
│   ├── shared/               # Cross-feature primitives
│   │   ├── data.py           #   yfinance data fetching (daily + intraday)
│   │   ├── indicators.py     #   RSI, MACD, Bollinger, EMA, ATR, VWAP
│   │   ├── pivots.py         #   Classic + Camarilla pivots
│   │   ├── holidays.py, costs.py, types.py
│   ├── notify/               # Alert channels (see "Alert channels" below)
│   │   ├── alerts.py         #   Telegram + WhatsApp (Twilio) send + formatters
│   │   ├── channels.py       #   whatsapp_enabled() + per-service telegram_config()
│   │   └── telegram_bot.py   #   Telegram command long-poller
│   ├── market_intel/         # sentiment.py (FinBERT/VADER) + news_monitor.py
│   ├── execution/            # broker.py (Zerodha Kite)
│   ├── emcure/               # The EMCURE trading engine
│   │   ├── intraday.py       #   SMA7 gap strategy, ORB, rupee targets
│   │   ├── managed_cycle.py  #   Managed-cycle auto-trader
│   │   ├── supertrend.py, strategy.py, scoring.py, predictor.py
│   │   ├── trade_manager.py  #   Manual trade state (T1/T2/T3/SL)
│   │   ├── probability.py, backtest.py, events.py, state.py, dashboard.py
│   ├── radar/                # Multi-stock opportunity radar (read-only scanner)
│   ├── swing/                # Swing-bot research lab (gated FAIL — do not deploy)
│   └── crypto/               # Crypto data / signals / messages / reversion lab / outcomes
├── deploy/
│   ├── oracle_setup.sh       # Full Oracle Cloud deployment (run once on server)
│   ├── *.service             # systemd units — ExecStart=python3 -m apps.<name>
│   ├── nginx.conf            # nginx reverse proxy template
│   └── deploy.sh             # Legacy DigitalOcean deploy script
├── scripts/telegram_chat_id.py  # Helper: resolve each bot's chat id
├── trade_state.json          # Runtime trade state — gitignored
├── strategy_state.json       # Supertrend strategy state — gitignored
├── requirements-core.txt     # Minimal deps for server (no torch/FinBERT)
├── requirements.txt          # Full deps including FinBERT
├── .env                      # Secrets — gitignored
└── .env.example
```

> **Import layout:** entry points live in `apps/` and are launched as modules
> (`python -m apps.main_headless`) so the repo root is on `sys.path` and
> `import src.*` resolves. Library code imports absolutely from its feature
> package, e.g. `from src.shared.data import fetch_daily`,
> `from src.notify.alerts import send_alert`, `from src.emcure.managed_cycle import step`.

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
| `SELL` | Close manual trade at the live price, show final P&L |
| `SELL 1710` | Close at an explicit price (fallback when the live quote fails) |
| `STATUS` | Live P&L + level progress |
| `EXIT` | Queue a managed-cycle sell — the tracker exits the position on its next cycle |
| `HALT` | Pause managed-cycle re-entries (exits still act) until `RESUME` |
| `RESUME` | Re-enable managed-cycle re-entries |
| `HELP` | Command list |

Same commands work on the emcure Telegram bot (`/status`, `/exit`, …).

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

# ── Telegram (PRIMARY channel) ──
TELEGRAM_TOKEN=           # Shared bot — fallback for any service without its own
TELEGRAM_CHAT_ID=
# Per-service bots — isolate the three feeds. Blank → falls back to shared above.
TELEGRAM_EMCURE_TOKEN=    # emcure: main_headless.py + bot_server.py (commands)
TELEGRAM_EMCURE_CHAT_ID=
TELEGRAM_RADAR_TOKEN=     # radar:  radar_headless.py (multi-stock scanner)
TELEGRAM_RADAR_CHAT_ID=
TELEGRAM_CRYPTO_TOKEN=    # crypto: crypto_headless.py
TELEGRAM_CRYPTO_CHAT_ID=

WHATSAPP_ENABLED=false    # OPT-IN fan-out to Twilio WhatsApp (default off)

HEADLESS=true             # Set true on server

# ── Managed-cycle auto-trader (replaces Supertrend when enabled) ──
MANAGED_CYCLE=false       # true → run managed-cycle, DISABLE Supertrend for the symbol
MANAGED_CYCLE_LIVE=false  # false → dry-run (announces decisions, NO real orders)
MANAGED_TARGETS=15,20,30  # rupee targets from entry; sells at highest reachable today
MANAGED_SL=100            # stop = entry − ₹100
MANAGED_QTY=8             # re-entry position size (shares)
MANAGED_REENTRY_GAP=20    # re-enter when price ≥ ₹20 below the 7-day SMA
MANAGED_REENTRY_GAP_PCT=0 # opt-in: >0 → trigger = SMA7 × pct/100 (scale-invariant,
                          # replaces the ₹ gap; the radar's SMA7 threshold is 1.4%)
MANAGED_REACH_MIN_PROB=50 # aim for the highest target with reach-prob ≥ this %
                          # (dynamic, from live price + 7/14/30-day moves)
# Live-safety guards (Phase 2):
MANAGED_MAX_DAILY_LOSS=   # ₹ realized-loss cap/day → halts re-entries (default sl×qty)
MANAGED_REENTRY_COOLDOWN_MIN=60   # min minutes between an exit and the next entry
MANAGED_BLOCK_REENTRY_AFTER_STOP=true  # no re-entry the same day after a stop-out
```

**Alert channels** — resolved centrally in `src/notify/channels.py`:
- **Telegram is primary.** Each service owns a dedicated bot so the three feeds stay separate:
  `emcure` (main_headless + bot_server commands), `radar` (radar_headless), `crypto` (crypto_headless).
  Per-service `TELEGRAM_<SERVICE>_TOKEN` / `_CHAT_ID` override the shared `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`;
  any blank value falls back to the shared bot, so a single-bot setup still works unchanged.
  (Telegram is periodically govt-blocked in India — `src/notify/alerts.py` has a circuit breaker.)
- **WhatsApp** is an **opt-in fan-out**, off by default. Set `WHATSAPP_ENABLED=true` (plus Twilio creds)
  to additionally mirror every alert to WhatsApp (50/day trial cap; over-limit sends silently fail).

---

## Running Locally

```bash
# Full interactive dashboard  (run entry points as modules from the repo root)
python -m apps.main

# Headless (alerts only, no Rich UI)
HEADLESS=true python -m apps.main_headless

# CLI trade management
python -m apps.trade buy 1693
python -m apps.trade sell
python -m apps.trade status
python -m apps.trade report      # P&L ledger: win-rate / profit-factor / expectancy

# WhatsApp bot (local dev with ngrok) — also serves GET /dashboard (read-only ops page)
./start_bot.sh

# Dead-man's-switch check (alerts if the tracker's heartbeat is stale in market hours)
python -m apps.watchdog
```

---

## Reliability & Observability

- **Atomic state writes** — all runtime JSON (`trade_state.json`, `managed_state.json`,
  `strategy_state.json`) goes through `src/shared/atomic_json.py` (temp + fsync + `os.replace`,
  plus an `fcntl.flock` guard on `trade_state.json` since both `bot_server` and `main_headless`
  write it). A crash/race mid-write can no longer truncate state and erase a live position.
- **Heartbeat + watchdog** — `main_headless` writes `src/shared/heartbeat.py` each loop;
  `apps/watchdog.py` (a `oneshot` on the `emcure-watchdog.timer`, every 5 min, self-gated to
  market hours) alarms on the emcure Telegram bot if the heartbeat goes stale. `update.sh`
  installs/enables the timer automatically.
- **Durable P&L ledger — NET of charges** — `src/emcure/ledger.py` (SQLite `emcure.db`, WAL,
  gitignored) records one row per closed round-trip with `charges` (STT/txn/stamp/GST + DP via
  `src/shared/costs.round_trip_charges`) and `net_pnl`; all analytics, the EOD "Day P&L", the
  Friday weekly digest, and the managed-cycle daily-loss kill-switch run on NET money. Managed
  exits log via `_record_exit`; manual sells via `apps/trade.py` + the bot commands.
  `python -m apps.trade report` prints the analytics.
- **Resting-stop ratchet** — in live mode the managed cycle's resting exchange SL order is
  lifted to the touched-target floor (`_ensure_protective_stop` cancel/re-places on a higher
  `stop_trigger`), so "never give a touched rung back" is enforced by the exchange even if the
  bot is offline between 5-min cycles. The trigger only ever moves up.
- **Command layer** — bot commands live in `src/emcure/commands.py` (unit-tested, no Flask
  import); `bot_server.py` is transport only. `live_price()` prefers the Kite LTP (the price
  the engine trades on) over the ~15-min-delayed yfinance quote for STATUS/SELL/dashboard.
- **Multi-component watchdog** — the tracker beats the default `heartbeat.json`; the bot's
  Telegram poller beats `heartbeat-emcure-bot.json` (`heartbeat.component_path`). The watchdog
  alarms on a stale tracker (or missing) and a stale bot beat — the EXIT/SELL command channel
  is a risk control, so its death must page.
- **Nightly backups** — `deploy/backup.sh` (emcure-backup.timer, 17:00 IST, installed by
  `update.sh`) snapshots `emcure.db`/`radar.db` (WAL-safe) + state JSONs to
  `/var/backups/emcure` (rotate 14); optional off-box copy via `BACKUP_OCI_BUCKET` or
  `BACKUP_RCLONE_REMOTE` in `/etc/default/emcure-backup`.
- **Market-hours deploy guard** — `update.sh` refuses to run Mon–Fri 09:15–15:30 IST (exits
  non-zero so the GitHub Action goes visibly red); override with `FORCE=1` for emergencies.
- **Web dashboard** — `GET /dashboard` on `bot_server` (gated by `HEALTH_API_KEY` in prod, open
  in local dev) renders heartbeat status, open position + live P&L, and ledger stats. Pure
  renderer in `src/emcure/dashboard_web.py`.
- **CI gate** — `.github/workflows/ci.yml` runs pytest + ruff on every push/PR; the deploy
  workflow's `deploy` job `needs: test`, so a failing suite blocks production.
- **Scheduled-alert windows** — the pre-open/post-open/EOD window boundaries AND the
  market-open predicate (`schedule.is_market_open`, weekday + holiday + 9:15–15:30) live only in
  `src/emcure/schedule.py` (pure predicates), consumed by `main.py`, `main_headless.py`,
  `watchdog.py`, and the `/dashboard`.
- **Persistent alert dedupe** — `src/emcure/alert_log.py` (`alerts_sent.json`, gitignored)
  write-through persists the tracker's `last_alerted` map and prunes previous days on load, so a
  mid-day restart/deploy can't re-send the pre-open briefing or the day's BUY signal. The EOD
  summary's Day P&L / trades-today come from `ledger.day_stats` (live trades only).
- **Remote managed-cycle control** — `EXIT` queues a sell (flag in `managed_state.json`,
  consumed by the tracker's next step), `HALT`/`RESUME` gate re-entries via `reentry_blocked`.
  `managed_state.json` therefore has two writers (tracker + bot_server), so every mutation in
  `managed_cycle.py` holds the `fcntl.flock` guard, like `trade_state.json`.
- **Lint/format** — `pyproject.toml` configures ruff (`ruff check src apps tests`); deps are
  range-capped in `requirements-core.txt` to stop breaking majors (e.g. yfinance) on fresh deploys.

---

## Deploying to Oracle Cloud

```bash
# SSH into server
ssh -i emcurekey ubuntu@<SERVER_IP>

# First-time setup (run once)
curl -fsSL https://raw.githubusercontent.com/heramb71/EmcurePriceTracker/main/deploy/oracle_setup.sh -o setup.sh
sudo bash setup.sh

# Update after code changes — one command: sync main, refresh deps,
# reinstall any drifted systemd units, daemon-reload, restart all services.
sudo bash /opt/emcure/deploy/update.sh
```

`deploy/update.sh` is the single deploy entry point (used both by hand and by
the **Deploy to Oracle Cloud** GitHub Action, which SSHes in and runs the exact
same script). It discovers every service running from `/opt/emcure` by
`WorkingDirectory`, so it restarts tracker/bot/radar/crypto without hardcoding
names, and re-installs a unit file whenever its `ExecStart` drifts (e.g. the
`apps/` restructure) — which a plain `git pull` would miss. `.env` and runtime
state (`trade_state.json`, `strategy_state.json`, `radar.db`) are gitignored and
never touched by the hard reset.

> GitHub Action secrets: `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`, `SSH_PORT`
> (optional). The deploy user needs passwordless `sudo` for `update.sh`. The
> script must already exist on the server (first-time setup or one manual
> deploy); thereafter it self-updates when it syncs the repo.

**Key Oracle Cloud gotchas:**
- iptables REJECT rule is at position 5 — insert ACCEPT rules with `-I INPUT 5`, not `-A`
- VCN Security List must also have ports 80/443 open (two separate firewalls)
- Use `screen` or `tmux` for long-running SSH commands (pip install takes 5+ min)
- Run setup script as file (`sudo bash setup.sh`), not piped (`curl | sudo bash`) — stdin breaks `read` prompts

---

## NSE Trade Opportunity Radar (`src/radar/`, `apps/radar_headless.py`, `apps/radar.py`)

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
- `features.py` — scalar per-stock snapshot (reuses `src/shared/data.py` + `src/shared/indicators.py`)
- `regime.py` — NIFTY regime: 50-DMA slope + ADX(14) + universe breadth → TRENDING_BULL/BEAR/SIDEWAYS
- `signals.py` — 5 detectors: SMA7 reversion, VWAP pullback, RVOL reversal, ATR breakout, gap reversion
- `scoring.py` — 0–100 confidence (RVOL/SMA7/VWAP/ATR/RSI/RS/regime), `SCORE_GATE=75`
- `scan.py` — pure pipeline → ranked, scored hits
- `dispatch.py` — cooldown + daily budget + digest batching (anti-flood)
- `alert_format.py` — the 🚨 TRADE OPPORTUNITY message + digest + `format_eod_stock` (per-stock EOD summary)
- `store.py` — SQLite (`radar.db`, gitignored): `signals` + `outcomes` tables
- `tracker.py` — evaluate matured outcomes at 1h/4h/1d/3d/5d/10d → MFE/MAE, WIN/LOSS/NEUTRAL
- `analytics.py` — win-rate / profit factor / expectancy by stock·signal·regime; leaders by
  expectancy; `muted_combos`/`validated_combos` let the outcomes act on alerting: a
  (stock, signal) combo with ≥ `RADAR_MUTE_MIN_N` (20) decided outcomes and negative
  expectancy goes silent (still recorded via a shadow gate, so the verdict can flip back);
  proven-positive combos get a "📈 Validated" tag in their alerts. `RADAR_MUTE_NEGATIVE=true`.

**Persistence:** one SQLite file (`radar.db`), stdlib `sqlite3`, WAL mode — no
server, OCI-free-tier friendly. The radar is the sole writer.

**Run:**
```bash
python -m apps.radar scan-now      # one scan, ranked table (no alerts/writes)
python -m apps.radar outcomes      # force a matured-outcome sweep
python -m apps.radar report        # analytics dashboard
python -m apps.radar_headless      # the service (market-aware loop)
```

**Deploy (separate service, leaves emcure-tracker/emcure-bot untouched):**
```bash
sudo cp /opt/emcure/deploy/radar.service /etc/systemd/system/emcure-radar.service
sudo systemctl daemon-reload && sudo systemctl enable --now emcure-radar
tail -f /var/log/emcure/radar.log
```

Config lives under the `RADAR_*` keys in `.env` (see `.env.example`). Telegram
only — uses the `radar` bot (`TELEGRAM_RADAR_TOKEN` / `TELEGRAM_RADAR_CHAT_ID`,
falling back to the shared `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`). See `src/notify/channels.py`.

**End-of-day summaries:** after market close on each trading day the radar sends
one per-stock EOD summary (OHLC, RSI/MACD/regime, tomorrow's SMA7 reversion watch
zone) in the EMCURE house style — `RADAR_EOD_SUMMARY=true` (default), excluding
`RADAR_EOD_EXCLUDE` (default `EMCURE`, which has its own managed EOD from
emcure-tracker). Watch zones are percentage-based (locked to the SMA7 signal's
1.4% threshold) so they scale across the ₹70–₹1800 price range; the summary is
watch-only and carries no tomorrow-probability claim (the reversion edge isn't
validated outside EMCURE).

---

## Crypto (`src/crypto/`, `apps/crypto_headless.py`)

BTC/ETH tracker: 8 AM / 8 PM briefings + oversold/overbought signal alerts
(Telegram `crypto` bot). **Alert-only — never trades.** Two research layers gate
any future crypto execution:

- **Reversion lab** — `python -m apps.crypto_reversion_lab` backtests EMCURE-style
  SMA7 dip-buying (percentage gaps/targets, multi-day holds, `src/crypto/reversion.py`)
  under the Indian VDA cost model (`src/crypto/costs.py`: exchange fees + 30%+cess
  per winner with **no loss set-off**; 1% TDS excluded as advance tax).
  **Gated FAIL (2026-07-03, ~7y history): do not build crypto execution.**
  BTC has no edge even gross (best PF ≈ 1.07). ETH has a real gross edge
  (gap ≥7% → +5% target: PF 1.58, +1.26%/trade, 69% WR, n=32) that survives fees
  (PF 1.38) and **dies at the tax layer** (PF 0.95, −0.12%/trade) — the no-offset
  VDA tax alone flips it negative.
- **Outcome tracking** — every fired crypto alert is recorded to `crypto.db`
  (WAL, gitignored; sole writer crypto_headless) and scored at 1d/3d/7d forward
  horizons (`src/crypto/outcomes.py`, WIN/LOSS thresholds ±1.5/3/5%).
  `python -m apps.crypto_outcomes` prints expectancy by symbol × alert-type ×
  horizon. Judge combos only at n≥20, same discipline as the radar.

---

## KittyBot — intraday single-stock trader (`src/kittybot/`, `apps/kittybot_headless.py`)

A **separate, self-contained** intraday trader that **picks ONE stock per day**
from a pre-ranked *kitty* (`daily_picks.json`, written by a separate pre-market
screener) and trades its opening-range breakout to a 2–5% target. **This is the
"Radar Bot" from the build spec — deliberately NOT named `radar` because
`src/radar/` is already the read-only scanner.** KittyBot *does* place orders, but
only through a pluggable broker and only when explicitly flagged live; otherwise
it **paper-trades** and journals everything.

**Daily flow** (state machine in `engine.py`, driven by `step(now)` each tick):
1. **09:15 prepare** — load the kitty; skip the day if it's stale (>24h) or a
   loss-streak halt is active; discard picks with earnings today or an opening
   gap > 1.5% vs prev close.
2. **09:30+ enter** — VIX rail (skip if India VIX up >15% intraday), build each
   survivor's 15-min opening range, take the **single strongest** breakout (LONG
   above the range high / SHORT below the low, and only when
   `short_room ≥ long_room`) on above-average volume, size to **≤1% capital
   risk**, place ONE entry. No trigger by 10:30 → no trade.
3. **manage** — ratchet the stop to breakeven at +1%; exit on target/stop; **hard
   time-exit at 15:10 IST** regardless of P&L. One position/day, no re-entry.

**Modules** (risk-bearing logic is pure + unit-tested in isolation):
- `config.py` — every threshold, from `config/kittybot.toml` (+ env override of
  the deploy/safety knobs); `picks.py` — load/parse the kitty or fall back to the
  configured universe; `filters.py` — earnings/gap discards; `opening_range.py` —
  build the range + breakout triggers; `selection.py` — the single-trade pick;
  `risk.py` — sizing, levels, breakeven, exits, P&L; `safety.py` — VIX/staleness/
  loss-streak circuit breakers; `state.py` — crash-safe position + streak +
  halt (atomic_json); `journal.py` — append-only per-day decision log
  (`kittybot_journal/kittybot-YYYY-MM-DD.jsonl`, **every skip logged**);
  `broker.py` — `Broker` protocol + `PaperBroker` (default) / `KiteBroker` (MIS) /
  `UpstoxBroker`; `marketdata.py` — the only yfinance boundary; `engine.py` — the
  orchestrator.

**Telegram alerts** — `src/kittybot/notify.py`. KittyBot **reuses the `radar`
Telegram feed** (`TELEGRAM_RADAR_TOKEN`/`_CHAT_ID`, via `channels.telegram_config`)
— it inherits the bot you already watch, so retiring the scanner just changes what
that bot sends, no new channel/creds. It pushes the decisions it *takes* (not
radar's "review manually" ideas): the morning kitty (post-filter watchlist), skips
(VIX / stale / halt / no-breakout) so a quiet day is explained, the single entry
(symbol/direction/entry/target/stop/qty), the breakeven move, and the exit with
P&L. Telegram-only, matching radar. The `KittyNotifier` is optional/injected —
`None` (or an unconfigured channel) degrades to journal-only, so alerts never block
trading. The feed is set by `telegram_service` in `config/kittybot.toml`
(default `"radar"`).

**Safety posture:** paper by default. `make_broker` refuses to build a live
broker unless `KITTYBOT_LIVE=true` **and** `KITTYBOT_BROKER` is `kite`/`upstox`.
All thresholds are checked-in config; `daily_picks.json`, `kittybot_state.json`,
and `kittybot_journal/` are runtime artifacts (gitignored).

**Pre-market screener** — `src/kittybot/screener.py` + `apps/kitty_screener.py`.
The "separate screener" that produces the ranked kitty. At 08:45 IST (via
`emcure-kitty-screener.timer`) it scans the kitty universe (`fallback_universe` in
the config), computes per-symbol **liquidity** (ADTV gate, ₹100 Cr default),
**volatility** (ATR%, avg daily range%), and **2%-reachability** (`hit_rate_2pct`
+ directional `long_room`/`short_room` = % of the last 60 sessions a 2% move was
available), scores 0–100 (2%-reachability-dominant), and atomically writes the
top-`max_picks` to `daily_picks.json`. Metric functions are pure (take a daily
frame) and unit-tested; `suggested_target_pct` = ~60% of the typical daily range
clamped to 2–5%, stop = target ÷ reward:risk. If the screener is missing/stale the
bot degrades to the fallback universe (breakout-only, no ranking) — a screener
failure never blocks the day, it just costs the ranked edge.

**Run / deploy:**
```bash
python -m apps.kitty_screener             # write daily_picks.json (ranked kitty)
python -m apps.kitty_screener --dry-run   # print the ranking, write nothing
python -m apps.kittybot_headless          # the trading service (paper by default)
python -m pytest tests/kittybot           # 99 unit + integration tests
# Deploy as its own service (leaves emcure-tracker/crypto untouched):
sudo cp /opt/emcure/deploy/kittybot.service /etc/systemd/system/emcure-kittybot.service
sudo systemctl daemon-reload && sudo systemctl enable --now emcure-kittybot
# update.sh then auto-installs the 08:45 screener timer whenever the kittybot
# service is present (deploy/kitty_screener.{service,timer}).
```

**Replacing the radar scanner** — KittyBot supersedes the read-only `src/radar/`
scanner (which alerted trade *ideas* for manual review but never traded). The
swap is at the **deployment level only** — the radar code + `radar.db` outcome
history stay in the repo, dormant, so it's reversible:
```bash
# on the server, after 15:30 IST (update.sh refuses market hours):
sudo systemctl disable --now emcure-radar
sudo rm /etc/systemd/system/emcure-radar.service   # so update.sh won't rediscover it
sudo bash /opt/emcure/deploy/update.sh             # installs/starts emcure-kittybot + screener
```
KittyBot reuses the radar Telegram bot (`TELEGRAM_RADAR_TOKEN`/`_CHAT_ID`), so once
the scanner service is off, the same bot simply starts sending KittyBot's alerts —
no `.env` change needed.
`update.sh` discovers services by `WorkingDirectory`, so once `emcure-radar` is
removed it stops being managed, and `emcure-kittybot` + the screener timer take
over. To restore radar, re-copy `deploy/radar.service` and re-enable it.

---

## src/emcure/trade_manager.py

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

## src/emcure/predictor.py — Message Formatters

```python
format_pre_open_briefing(...)   # 9:00 AM briefing
format_post_open_briefing(...)  # 9:20 AM ORB update
format_eod_summary(...)         # 3:30 PM EOD close message
format_confidence_line(...)     # Single-line dashboard embed
```

---

## Coding Conventions

- Organize `src/` by feature/domain: `shared`, `notify`, `market_intel`, `execution`, `emcure`, `radar`, `swing`, `crypto`. Cross-feature code belongs in `shared`.
- Entry points live in `apps/` and import library code absolutely (`from src.<feature>.<module> import ...`); run them as modules (`python -m apps.<name>`).
- All indicator functions are pure — take Series/DataFrame, return scalar or dict
- All network calls return `None`/empty on failure — never raise to caller
- Files: max 400 lines. Functions: max 50 lines
- No hardcoded prices or symbols outside the `apps/main.py` config block
- `trade_state.json` and `strategy_state.json` are runtime state — never commit
