"""
24-hour rolling news sentiment monitor.

Runs as a background thread; the main loop reads its state at any time.
Detects sentiment regime shifts (e.g. Neutral → Strongly Bearish within 4 hrs)
and exposes them as WhatsApp-ready alert strings.

Usage:
    monitor = NewsMonitor()
    monitor.start()           # launches background thread

    # in main loop:
    snapshot = monitor.snapshot()
    if snapshot["shift_alert"]:
        send_whatsapp_alert(..., snapshot["shift_alert"])
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# How often to refetch news (seconds)
FETCH_INTERVAL_SECS = 1800  # 30 min

# Rolling window kept in memory
WINDOW_HOURS = 24

# Sentiment shift threshold: if 4-hr avg changes by this much → alert
SHIFT_THRESHOLD = 0.25

# Article bucket size for timeline display
BUCKET_HOURS = 6

_LABEL_MAP = {
    (0.15,  1.0):  ("Strongly Bullish", "🟢"),
    (0.05,  0.15): ("Bullish",          "🟢"),
    (-0.05, 0.05): ("Neutral",          "🟡"),
    (-0.15,-0.05): ("Bearish",          "🔴"),
    (-1.0, -0.15): ("Strongly Bearish", "🔴"),
}


def _label(score: float) -> tuple[str, str]:
    for (lo, hi), (label, emoji) in _LABEL_MAP.items():
        if lo <= score < hi:
            return label, emoji
    return "Neutral", "🟡"


# ─────────────────────────────────────────────────────────────────────────────
# Monitor
# ─────────────────────────────────────────────────────────────────────────────

class NewsMonitor:
    """
    Thread-safe 24-hour rolling news sentinel.

    Each article stored as:
        { title, published, score, label, emoji, fetched_at (datetime) }
    """

    def __init__(
        self,
        rss_url: str = (
            "https://news.google.com/rss/search"
            "?q=Emcure+Pharmaceuticals+stock&hl=en-IN&gl=IN&ceid=IN:en"
        ),
        max_articles_per_fetch: int = 15,
        sentiment_model=None,
    ) -> None:
        self._rss_url    = rss_url
        self._max_fetch  = max_articles_per_fetch
        self._model      = sentiment_model
        self._lock       = threading.Lock()
        self._articles: deque = deque()  # deque of dicts, newest last
        self._last_fetch: Optional[datetime] = None
        self._shift_alert: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch background fetch thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="news-monitor", daemon=True
        )
        self._thread.start()
        logger.info("NewsMonitor started")

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> dict:
        """
        Return a thread-safe snapshot of the current 24-hour state.

        Keys:
            articles_24h  list of article dicts
            avg_score     float  overall 24h avg
            label         str    e.g. "Bullish"
            emoji         str
            counts        {bullish, bearish, neutral}
            buckets       list of 4 × 6-hour buckets (oldest → newest)
            shift_alert   str | None  — cleared after first read
            last_fetch    str  datetime of last successful fetch
        """
        with self._lock:
            alert      = self._shift_alert
            self._shift_alert = None             # consume once
            articles   = list(self._articles)   # copy
            last_fetch = self._last_fetch

        now      = datetime.utcnow()
        cutoff   = now - timedelta(hours=WINDOW_HOURS)
        recent   = [a for a in articles if a["fetched_at"] >= cutoff]

        avg_score = (
            sum(a["score"] for a in recent) / len(recent) if recent else 0.0
        )
        label, emoji = _label(avg_score)

        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        for a in recent:
            if a["score"] > 0.05:
                counts["bullish"] += 1
            elif a["score"] < -0.05:
                counts["bearish"] += 1
            else:
                counts["neutral"] += 1

        # 6-hour buckets (oldest first)
        buckets = []
        for b in range(4):
            b_start = cutoff + timedelta(hours=b * BUCKET_HOURS)
            b_end   = b_start + timedelta(hours=BUCKET_HOURS)
            bucket_arts = [a for a in recent if b_start <= a["fetched_at"] < b_end]
            if bucket_arts:
                b_avg   = sum(a["score"] for a in bucket_arts) / len(bucket_arts)
                b_label, b_emoji = _label(b_avg)
            else:
                b_avg, b_label, b_emoji = 0.0, "No data", "⚪"

            start_h = (b_start.hour) % 24
            end_h   = (b_end.hour)   % 24
            buckets.append({
                "label": f"{start_h:02d}:00–{end_h:02d}:00",
                "avg_score": round(b_avg, 3),
                "sentiment": b_label,
                "emoji": b_emoji,
                "count": len(bucket_arts),
            })

        return {
            "articles_24h": recent[-10:],   # last 10 for display
            "total_count":  len(recent),
            "avg_score":    round(avg_score, 3),
            "label":        label,
            "emoji":        emoji,
            "counts":       counts,
            "buckets":      buckets,
            "shift_alert":  alert,
            "last_fetch":   (
                last_fetch.strftime("%H:%M") if last_fetch else "—"
            ),
        }

    # ── Background loop ───────────────────────────────────────────────────────

    def _loop(self) -> None:
        # Fetch once immediately, then every FETCH_INTERVAL_SECS
        self._fetch_and_update()
        while not self._stop_event.is_set():
            self._stop_event.wait(FETCH_INTERVAL_SECS)
            if not self._stop_event.is_set():
                self._fetch_and_update()

    def _fetch_and_update(self) -> None:
        try:
            new_articles = self._fetch_articles()
            if not new_articles:
                return

            with self._lock:
                existing_titles = {a["title"] for a in self._articles}
                added = []
                for art in new_articles:
                    if art["title"] not in existing_titles:
                        self._articles.append(art)
                        added.append(art)

                # Prune articles older than 24 h
                cutoff = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
                while self._articles and self._articles[0]["fetched_at"] < cutoff:
                    self._articles.popleft()

                self._last_fetch = datetime.utcnow()

                if added:
                    self._maybe_set_shift_alert()

        except Exception:
            logger.exception("NewsMonitor._fetch_and_update failed")

    def _fetch_articles(self) -> list[dict]:
        """Fetch and score news articles."""
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser not installed; skipping news fetch")
            return []

        try:
            feed = feedparser.parse(self._rss_url)
        except Exception:
            logger.exception("feedparser.parse failed")
            return []

        now = datetime.utcnow()
        articles = []
        for entry in feed.entries[: self._max_fetch]:
            title = entry.get("title", "")
            score = self._score_text(title)
            label, emoji = _label(score)
            articles.append({
                "title":      title[:120],
                "published":  entry.get("published", ""),
                "score":      score,
                "label":      label,
                "emoji":      emoji,
                "fetched_at": now,
            })
        return articles

    def _score_text(self, text: str) -> float:
        """Score text with the loaded model or fall back to VADER."""
        if self._model is not None:
            try:
                return float(self._model.score(text))
            except Exception:
                pass

        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            sia = SentimentIntensityAnalyzer()
            return float(sia.polarity_scores(text)["compound"])
        except ImportError:
            pass

        # Last resort: keyword count
        bull_words = {"rise", "gain", "rally", "beat", "profit", "bullish",
                      "growth", "strong", "upgrade", "buy"}
        bear_words = {"fall", "drop", "loss", "miss", "bearish", "decline",
                      "weak", "downgrade", "sell", "concern"}
        words = set(text.lower().split())
        b = len(words & bull_words)
        br = len(words & bear_words)
        if b == br:
            return 0.0
        return 0.2 if b > br else -0.2

    def _maybe_set_shift_alert(self) -> None:
        """
        Detect a sentiment regime change in the last 4 hours vs the prior 4.
        Called inside the lock — do not acquire again.
        """
        now    = datetime.utcnow()
        t4     = now - timedelta(hours=4)
        t8     = now - timedelta(hours=8)
        cutoff = now - timedelta(hours=WINDOW_HOURS)

        arts_recent = [a for a in self._articles if a["fetched_at"] >= t4]
        arts_prior  = [a for a in self._articles if t8 <= a["fetched_at"] < t4]

        if len(arts_recent) < 2 or len(arts_prior) < 2:
            return

        avg_recent = sum(a["score"] for a in arts_recent) / len(arts_recent)
        avg_prior  = sum(a["score"] for a in arts_prior)  / len(arts_prior)
        delta      = avg_recent - avg_prior

        if abs(delta) < SHIFT_THRESHOLD:
            return

        direction = "⬆️ Bullish shift" if delta > 0 else "⬇️ Bearish shift"
        lbl_recent, emoji_recent = _label(avg_recent)
        lbl_prior,  emoji_prior  = _label(avg_prior)

        self._shift_alert = (
            f"📰 *Sentiment Shift Detected*\n"
            f"{direction}  (Δ {delta:+.2f})\n"
            f"Last 4h: {emoji_recent} {lbl_recent} ({avg_recent:+.2f})\n"
            f"Prior 4h: {emoji_prior} {lbl_prior} ({avg_prior:+.2f})\n"
            f"Recent headline: \"{arts_recent[-1]['title'][:80]}\""
        )
        logger.info("Sentiment shift alert set: %s", direction)


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard section builder
# ─────────────────────────────────────────────────────────────────────────────

def format_news_panel(snapshot: dict) -> str:
    """Plain-text representation for terminal display."""
    lines = [
        f"24h Sentiment: {snapshot['emoji']} {snapshot['label']}"
        f"  (score {snapshot['avg_score']:+.2f}  |  {snapshot['total_count']} articles)"
        f"  Last fetch: {snapshot['last_fetch']}",
        "",
    ]

    for b in snapshot["buckets"]:
        bar = "█" * int(abs(b["avg_score"]) * 10)
        lines.append(f"  {b['label']}  {b['emoji']} {b['sentiment']:<18}  {bar} ({b['count']} articles)")

    lines.append("")
    for art in snapshot["articles_24h"][-5:]:
        lines.append(f"  {art['emoji']}  {art['title'][:80]}")

    return "\n".join(lines)
