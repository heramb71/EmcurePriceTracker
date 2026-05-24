import os
from dotenv import load_dotenv

load_dotenv()

# ── Stock ──────────────────────────────────────────────────────────────────
STOCK_SYMBOL = "EMCURE.NS"          # yfinance symbol (NSE)
STOCK_SYMBOL_BSE = "544143.BO"      # yfinance BSE fallback
STOCK_NAME = "Emcure Pharmaceuticals"
SECTOR_INDEX_SYMBOL = "^CNXPHARMA"  # Nifty Pharma Index

# ── Refresh ────────────────────────────────────────────────────────────────
REFRESH_SECONDS = 300
FETCH_TIMEOUT = 20                  # seconds per fetch worker

# ── News ───────────────────────────────────────────────────────────────────
NEWS_QUERY = "Emcure Pharmaceuticals stock"
NEWS_RSS_SOURCES = [
    (
        "https://news.google.com/rss/search?q="
        + "+".join(NEWS_QUERY.split())
        + "&hl=en-IN&gl=IN&ceid=IN:en",
        "Google News",
    ),
    (
        "https://www.moneycontrol.com/rss/buzzingstocks.xml",
        "MoneyControl",
    ),
    (
        "https://economictimes.indiatimes.com/markets/stocks/rss.cms",
        "Economic Times",
    ),
]
MAX_NEWS_ITEMS = 10

# ── Sentiment ──────────────────────────────────────────────────────────────
FINBERT_MODEL = os.environ.get("FINBERT_MODEL_PATH", "ProsusAI/finbert")

# ── Alerts ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Alert thresholds
ALERT_RSI_OVERSOLD = 30.0
ALERT_RSI_OVERBOUGHT = 70.0
ALERT_DELIVERY_PCT_SPIKE = 60.0     # % above which delivery is "high conviction"
ALERT_SENTIMENT_FLIP_THRESHOLD = 0.10  # compound score change that counts as a flip

# ── Backtesting ────────────────────────────────────────────────────────────
BACKTEST_PERIOD_DAYS = 365          # how many days of history for backtesting

# ── Indicators ─────────────────────────────────────────────────────────────
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
EMA_SHORT = 20
EMA_LONG = 50
SR_LOOKBACK = 60                    # days to look back for S/R pivot detection
SR_MIN_TOUCHES = 2                  # min times price must touch a level
