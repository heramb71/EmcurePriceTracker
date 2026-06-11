from __future__ import annotations

import logging

import feedparser

from src.types import Article, SentimentResult

logger = logging.getLogger(__name__)

NEWS_RSS = (
    "https://news.google.com/rss/search?q=Emcure+Pharmaceuticals+EMCURE+stock"
    "&hl=en-IN&gl=IN&ceid=IN:en"
)
MAX_ARTICLES = 10

# Module-level singleton — populated by load_sentiment_model()
_model = None


class _FinBERTWrapper:
    def __init__(self, pipeline):
        self._pipeline = pipeline

    def score(self, text: str) -> float:
        try:
            result = self._pipeline([text[:512]])[0]
            label = result["label"].lower()
            conf = float(result.get("score", 1.0))
            if label == "positive":
                return round(conf, 3)
            elif label == "negative":
                return round(-conf, 3)
            return 0.0
        except Exception:
            logger.exception("FinBERT inference failed")
            return 0.0


class _VADERWrapper:
    def __init__(self, analyzer):
        self._analyzer = analyzer

    def score(self, text: str) -> float:
        try:
            return round(self._analyzer.polarity_scores(text)["compound"], 3)
        except Exception:
            return 0.0


class _NullWrapper:
    def score(self, text: str) -> float:
        return 0.0


def load_sentiment_model():
    global _model
    if _model is not None:
        return _model

    import os
    if os.getenv("FINBERT_MODEL_PATH", "").lower() == "skip":
        logger.warning("FINBERT_MODEL_PATH=skip — using VADER.")
    else:
        try:
            from transformers import pipeline as hf_pipeline

            model_path = os.getenv("FINBERT_MODEL_PATH") or "ProsusAI/finbert"
            pipe = hf_pipeline(
                "text-classification",
                model=model_path,
                tokenizer=model_path,
                truncation=True,
                max_length=512,
            )
            logger.warning("FinBERT loaded successfully.")
            _model = _FinBERTWrapper(pipe)
            return _model
        except Exception:
            logger.warning("FinBERT unavailable — falling back to VADER.")

    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        _model = _VADERWrapper(SentimentIntensityAnalyzer())
        return _model
    except Exception:
        logger.warning("VADER unavailable — sentiment will default to neutral.")
        _model = _NullWrapper()
        return _model


def fetch_news(rss_url: str = NEWS_RSS, max_items: int = MAX_ARTICLES) -> list[dict]:
    """Fetch and score articles using the loaded sentiment model."""
    model = _model or _NullWrapper()
    articles: list[dict] = []
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:max_items]:
            title = entry.get("title", "")
            pub = entry.get("published", "–")[:25]
            s = model.score(title)
            if s >= 0.05:
                label, color, icon = "Bullish", "green", "▲"
            elif s <= -0.05:
                label, color, icon = "Bearish", "red", "▼"
            else:
                label, color, icon = "Neutral", "yellow", "●"
            articles.append(
                {
                    "title": title[:120] + ("…" if len(title) > 120 else ""),
                    "published": pub,
                    "sentiment": label,
                    "score": round(s, 3),
                    "color": color,
                    "icon": icon,
                }
            )
    except Exception:
        logger.exception("fetch_news failed")
    return articles


def aggregate_sentiment(articles: list[dict]) -> dict:
    """
    Aggregate sentiment from multiple articles.

    Returns a dict for backward compatibility with main.py.
    Use SentimentResult.from_dict() to get a type-safe version.
    """
    if not articles:
        neutral_result = SentimentResult(
            label="Neutral",
            score=0.0,
            color="yellow",
            bullish=0,
            bearish=0,
            neutral=0,
            articles=[],
        )
        return neutral_result.to_dict()

    avg = sum(a["score"] for a in articles) / len(articles)
    bullish = sum(1 for a in articles if a["sentiment"] == "Bullish")
    bearish = sum(1 for a in articles if a["sentiment"] == "Bearish")
    neutral = sum(1 for a in articles if a["sentiment"] == "Neutral")

    if avg >= 0.15:
        label, color = "Strongly Bullish", "bold green"
    elif avg >= 0.05:
        label, color = "Bullish", "green"
    elif avg <= -0.15:
        label, color = "Strongly Bearish", "bold red"
    elif avg <= -0.05:
        label, color = "Bearish", "red"
    else:
        label, color = "Neutral", "yellow"

    # Create typed result and convert back to dict for backward compatibility
    result = SentimentResult(
        label=label,
        score=round(avg, 3),
        color=color,
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
        articles=[
            Article(
                title=a["title"],
                published=a["published"],
                sentiment=a["sentiment"],
                score=a["score"],
                color=a["color"],
                icon=a["icon"],
            )
            for a in articles
        ],
    )
    return result.to_dict()
