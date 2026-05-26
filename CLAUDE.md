# EmcurePriceTracker — Implementation Instructions

## What This Project Does

A terminal-based swing trading dashboard for NSE-listed stocks (default: Emcure Pharmaceuticals — `EMCUREPHARM.NS`). It computes **five signal systems**, combines them into a single scored recommendation, and optionally pushes **Telegram alerts** when entry zones are hit.

No paid API keys required — all market data comes from **yfinance**.

---

## Project Structure

```
EmcurePriceTracker/
├── main.py                  # Entry point. Live refresh loop. Orchestrates all modules.
├── src/
│   ├── __init__.py
│   ├── data.py              # yfinance data fetching (daily + intraday)
│   ├── indicators.py        # RSI, MACD, Bollinger Bands, EMA, ATR, VWAP
│   ├── pivots.py            # Classic Pivot Points + Camarilla Pivots
│   ├── sentiment.py         # FinBERT sentiment (VADER fallback) + Google News RSS
│   ├── scoring.py           # HMM market regime + combined signal scorer
│   ├── alerts.py            # Telegram alert dispatcher
│   └── dashboard.py         # Rich terminal UI (all panels)
├── requirements.txt
├── .env                     # Secrets (gitignored)
├── .env.example
├── .gitignore
└── .vscode/
    ├── launch.json
    ├── settings.json
    └── extensions.json
```

---

## Environment Variables (`.env`)

```
ALPHA_VANTAGE_KEY=           # NOT used — kept for legacy reference only
TELEGRAM_TOKEN=              # Optional: bot token from @BotFather
TELEGRAM_CHAT_ID=            # Optional: your chat/channel ID

FINBERT_MODEL_PATH=          # Optional: local path to FinBERT model
                             # Default: ProsusAI/finbert from HuggingFace (auto-downloaded)
```

---

## Module Specifications

### `src/data.py`

**Purpose:** Fetch all market data. Single source of truth for raw OHLCV.

**Functions:**

```python
fetch_daily(ticker: str, days: int = 100) -> pd.DataFrame
```
- Uses `yfinance.download(ticker + ".NS", period=f"{days}d", interval="1d")`
- Returns DataFrame with lowercase columns: `open, high, low, close, volume`
- Index is `DatetimeIndex`
- Must handle MultiIndex columns (yfinance quirk for some versions): flatten with `df.columns = df.columns.get_level_values(0)`

```python
fetch_intraday(ticker: str, interval: str = "5m", days: int = 5) -> pd.DataFrame
```
- Uses `yfinance.download(ticker + ".NS", period=f"{days}d", interval=interval)`
- Returns same column format
- Used for VWAP calculation only

```python
get_latest_quote(df_daily: pd.DataFrame) -> dict
```
- Extracts last row into quote dict:
  `{ price, open, high, low, close, volume, prev_close, change, change_pct, date }`

**Error handling:** All functions return `None` / empty DataFrame on failure — never raise.

---

### `src/indicators.py`

**Purpose:** Compute all technical indicators from OHLCV DataFrames.

**Functions:**

```python
compute_rsi(close: pd.Series, period: int = 14) -> float
```
- Standard Wilder's RSI. Returns last value.

```python
compute_macd(close: pd.Series) -> tuple[float, float, float]
```
- Returns `(macd_line, signal_line, histogram)` — all last values.
- EMA spans: 12, 26, signal: 9.

```python
compute_bollinger(close: pd.Series, period: int = 20) -> tuple[float, float, float]
```
- Returns `(upper, mid, lower)` — 2 standard deviations.

```python
compute_ema(close: pd.Series, span: int) -> float
```
- Returns last EMA value for given span.

```python
compute_atr(df: pd.DataFrame, period: int = 14) -> float
```
- True Range = max(H-L, |H-Cp|, |L-Cp|) where Cp = previous close.
- ATR = rolling mean of TR over `period` days.
- Returns last ATR value.

```python
compute_vwap(df_intraday: pd.DataFrame) -> float
```
- VWAP = cumsum(typical_price × volume) / cumsum(volume)
- Typical price = (H + L + C) / 3
- Computed on today's intraday bars only (filter by today's date)
- Returns current VWAP value

```python
compute_avg_volume(df: pd.DataFrame, days: int = 20) -> int
```
- Returns rolling 20-day average volume.

---

### `src/pivots.py`

**Purpose:** Compute all pivot-based support/resistance levels from previous day's OHLC.

**Input:** Previous day's `high`, `low`, `close` (floats).

**Functions:**

```python
classic_pivots(high: float, low: float, close: float) -> dict
```
Returns:
```python
{
  "PP": ...,
  "R1": ..., "R2": ..., "R3": ...,
  "S1": ..., "S2": ..., "S3": ...
}
```
Formulas:
```
PP = (H + L + C) / 3
R1 = 2×PP − L
R2 = PP + (H − L)
R3 = H + 2×(PP − L)
S1 = 2×PP − H
S2 = PP − (H − L)
S3 = L − 2×(H − PP)
```

```python
camarilla_pivots(high: float, low: float, close: float) -> dict
```
Returns:
```python
{
  "H4": ..., "H3": ...,
  "L3": ..., "L4": ...
}
```
Formulas:
```
H4 = C + 1.1 × (H − L) / 2   ← Short entry / strong resistance
H3 = C + 1.1 × (H − L) / 4   ← Long exit / resistance
L3 = C − 1.1 × (H − L) / 4   ← Short exit / support
L4 = C − 1.1 × (H − L) / 2   ← Long entry / strong support
```

```python
pivot_signal(price: float, pivots: dict) -> str
```
- Returns which zone the current price is in:
  `"Below S3"`, `"S3–S2"`, `"S2–S1"`, `"S1–PP"`, `"PP–R1"`, `"R1–R2"`, `"R2–R3"`, `"Above R3"`

```python
atr_levels(price: float, atr: float) -> dict
```
Returns:
```python
{
  "entry":  price,
  "sl":     round(price - 1.5 * atr, 2),
  "t1":     round(price + 1.5 * atr, 2),
  "t2":     round(price + 3.0 * atr, 2),
  "atr":    round(atr, 2)
}
```

---

### `src/sentiment.py`

**Purpose:** Score news sentiment using FinBERT. Falls back to VADER if torch is not available.

**Constants:**
```python
NEWS_RSS = "https://news.google.com/rss/search?q=Emcure+Pharmaceuticals+stock&hl=en-IN&gl=IN&ceid=IN:en"
MAX_ARTICLES = 10
```

**Functions:**

```python
load_sentiment_model() -> object
```
- Tries to load `pipeline("text-classification", model="ProsusAI/finbert")`.
- If `ImportError` or load fails → falls back to VADER `SentimentIntensityAnalyzer`.
- Returns a wrapper with `.score(text: str) -> float` method returning compound in [-1, 1].

```python
fetch_news(rss_url: str = NEWS_RSS, max_items: int = MAX_ARTICLES) -> list[dict]
```
- Parses Google News RSS with `feedparser`.
- Each article dict: `{ title, published, sentiment, score, color, icon }`
- `sentiment`: `"Bullish"` / `"Bearish"` / `"Neutral"` based on score threshold ±0.05.

```python
aggregate_sentiment(articles: list[dict]) -> dict
```
Returns:
```python
{
  "label": "Bullish",        # Strongly Bullish / Bullish / Neutral / Bearish / Strongly Bearish
  "score": 0.12,             # avg compound
  "color": "green",
  "bullish": 4, "bearish": 2, "neutral": 4
}
```

**FinBERT label mapping:**
- `"positive"` → +score
- `"negative"` → −score
- `"neutral"` → 0

---

### `src/scoring.py`

**Purpose:** Combine all signals into a single 0–1 score with a trade recommendation.

**Market Regime (HMM):**

```python
detect_regime(df: pd.DataFrame) -> str
```
- Features: daily returns + rolling 5-day volatility (std of returns).
- Fit `hmmlearn.GaussianHMM(n_components=3)` on the last 60 days.
- Label states by mean return: highest = `"Trending Up"`, lowest = `"Trending Down"`, middle = `"Ranging"`.
- Returns regime of the latest bar.
- Falls back to `"Unknown"` if hmmlearn unavailable or insufficient data.

**Signal Weights (default):**

| Signal | Weight |
|---|---|
| Pivot proximity (price near S1/S2 or R1/R2) | 0.25 |
| RSI zone | 0.20 |
| MACD histogram direction | 0.15 |
| VWAP relationship (price vs VWAP) | 0.20 |
| Sentiment score | 0.10 |
| Volume vs avg ratio | 0.10 |

**Regime weight adjustments:**
- `"Trending Up"` → boost MACD weight +0.05, reduce pivot weight −0.05
- `"Trending Down"` → boost RSI weight +0.05, reduce VWAP weight −0.05
- `"Ranging"` → boost pivot weight +0.10, reduce MACD weight −0.10

```python
compute_score(
    quote: dict,
    pivots: dict,
    cam: dict,
    atr_lvls: dict,
    rsi: float,
    macd_hist: float,
    vwap: float,
    ema20: float,
    ema50: float,
    sentiment: dict,
    avg_volume: int,
    regime: str
) -> dict
```

Returns:
```python
{
  "score":       0.72,          # 0.0 – 1.0
  "signal":      "Buy",         # Strong Buy / Buy / Hold / Sell / Strong Sell
  "signal_color": "green",
  "entry":       1145.00,
  "sl":          1110.00,
  "t1":          1180.00,
  "t2":          1215.00,
  "regime":      "Trending Up",
  "breakdown":   { "pivot": 0.8, "rsi": 0.6, ... }  # per-signal sub-scores
}
```

**Score → Signal mapping:**
```
≥ 0.70 → Strong Buy  (bold green)
≥ 0.55 → Buy         (green)
≤ 0.30 → Strong Sell (bold red)
≤ 0.45 → Sell        (red)
else   → Hold        (yellow)
```

---

### `src/alerts.py`

**Purpose:** Send Telegram message when a strong signal fires.

**Functions:**

```python
send_alert(token: str, chat_id: str, message: str) -> bool
```
- Uses `python-telegram-bot` (async) or `requests` POST to Telegram Bot API.
- Returns `True` on success, `False` on failure — never raises.

```python
format_alert(ticker: str, score_result: dict, quote: dict) -> str
```
- Returns a formatted Markdown string:
```
🚨 *EMCUREPHARM.NS — Strong Buy*
Price: ₹1145.00 (+1.2%)
Entry: ₹1145 | SL: ₹1110 | T1: ₹1180 | T2: ₹1215
Score: 0.72 | Regime: Trending Up
```

```python
should_alert(score_result: dict, last_alerted: dict) -> bool
```
- Returns `True` if signal is `"Strong Buy"` or `"Strong Sell"` AND last alert for that signal was > 30 minutes ago.
- `last_alerted` is a dict `{ signal: datetime }` maintained in `main.py`.

---

### `src/dashboard.py`

**Purpose:** Build the Rich terminal layout from all computed data.

**Panels:**

1. **Header** — Ticker, price, change %, date, last updated, refresh countdown.
2. **Price & Bands** — Current price, O/H/L, prev close, EMA20/50, Bollinger Bands.
3. **Pivot Levels** — Classic PP/S1–S3/R1–R3 table. Highlight which zone price is in.
4. **Camarilla Levels** — H3/H4/L3/L4 with role labels. Highlight nearest level.
5. **ATR Levels** — Entry / SL / T1 / T2 computed from ATR.
6. **VWAP & Volume** — VWAP vs price, EMA cross signal, volume bar vs avg.
7. **Technicals** — RSI + signal, MACD histogram, EMA cross.
8. **Sentiment** — News table (title, score, label), aggregate bar.
9. **Signal Box** — Large centered panel: score gauge (0–1), signal, entry/SL/T1/T2, regime.

**Layout (Rich `Layout`):**
```
┌─────────────── HEADER ───────────────────┐
│   PRICE & BANDS │ PIVOTS │ CAMARILLA     │
│   ATR LEVELS    │ VWAP   │ TECHNICALS    │
│──────── NEWS SENTIMENT ──────────────────│
│──────────── SIGNAL BOX ──────────────────│
```

**Function:**
```python
build_dashboard(quote, pivots, cam, atr_lvls, indicators, sentiment, score_result, last_updated, next_refresh_secs) -> Layout
```

---

### `main.py`

**Purpose:** Wire all modules, run the live refresh loop.

**Startup sequence:**
1. Load `.env` with `python-dotenv`.
2. Validate `TICKER` (default `"EMCUREPHARM"` — NSE suffix added by `data.py`).
3. Load sentiment model once (FinBERT is slow to load — do it before the loop).
4. Enter `rich.Live(screen=True)` loop.

**Each refresh cycle:**
```
1. fetch_daily()          → df_daily
2. fetch_intraday()       → df_intraday
3. get_latest_quote()     → quote
4. compute_atr()          → atr
5. classic_pivots()       → pivots  (uses prev day H/L/C)
6. camarilla_pivots()     → cam
7. atr_levels()           → atr_lvls
8. compute_rsi/macd/bb/ema/vwap → indicators
9. fetch_news()           → articles
10. aggregate_sentiment() → sentiment
11. detect_regime()       → regime
12. compute_score()       → score_result
13. should_alert()        → if True: send_alert()
14. build_dashboard()     → layout
15. live.update(layout)
16. Sleep REFRESH_SECONDS (default 300)
```

**Config (top of main.py or via `.env`):**
```python
TICKER          = os.getenv("TICKER", "EMCUREPHARM")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "300"))
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID= os.getenv("TELEGRAM_CHAT_ID", "")
```

---

## Dependencies

See `requirements.txt`. Key notes:

- **torch + transformers** — Only needed for FinBERT. If not installed, `sentiment.py` silently falls back to VADER. To skip torch entirely, remove from requirements and set `FINBERT_MODEL_PATH=skip` in `.env`.
- **hmmlearn** — Only needed for regime detection. Falls back to `"Unknown"` regime if unavailable.
- **vectorbt** — Reserved for future backtesting module. Not used in v1.
- **nsepython** — Reserved for live NSE market depth (future). Not used in v1.
- **python-dotenv** — Must be loaded first in `main.py` before any `os.getenv()` calls.

---

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Run (API key in .env)
python main.py

# Or override ticker
TICKER=RELIANCE python main.py
```

Press `Ctrl+C` to exit cleanly.

---

## Coding Conventions

- Follow `src/` module boundaries strictly — no cross-imports except via `main.py`.
- All indicator functions are **pure** — take Series/DataFrame, return scalar or dict.
- All network calls return `None`/empty on failure — never raise to caller.
- Rich panels in `dashboard.py` are pure functions — take data, return `Panel`.
- Files: max 400 lines. Functions: max 50 lines.
- No hardcoded prices or symbols outside `main.py` config block.
