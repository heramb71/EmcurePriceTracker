from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from emcure_tracker import config
from emcure_tracker.data.news import ArticleResult

logger = logging.getLogger(__name__)

# Label mapping from FinBERT outputs
_LABEL_MAP = {
    "positive": ("Bullish", "green", "▲"),
    "negative": ("Bearish", "red", "▼"),
    "neutral": ("Neutral", "yellow", "●"),
}
_SCORE_MAP = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


@dataclass(frozen=True)
class ScoredArticle:
    title: str
    source: str
    published: str
    sentiment: str
    color: str
    icon: str
    score: float                # -1.0 … +1.0


@dataclass(frozen=True)
class SentimentResult:
    label: str
    score: float
    color: str
    bullish: int
    bearish: int
    neutral: int
    articles: tuple[ScoredArticle, ...]


# ── FinBERT singleton ──────────────────────────────────────────────────────

class SentimentModel:
    _pipeline = None

    @classmethod
    def load(cls) -> None:
        if cls._pipeline is not None:
            return
        try:
            from transformers import pipeline as hf_pipeline
            logger.warning("Loading FinBERT model — this may take a moment on first run…")
            cls._pipeline = hf_pipeline(
                "text-classification",
                model=config.FINBERT_MODEL,
                tokenizer=config.FINBERT_MODEL,
                truncation=True,
                max_length=512,
            )
            logger.warning("FinBERT model loaded.")
        except Exception:
            logger.exception("Failed to load FinBERT; sentiment will be unavailable")
            cls._pipeline = None

    @classmethod
    def score(cls, texts: list[str]) -> list[dict]:
        if cls._pipeline is None:
            return [{"label": "neutral", "score": 0.0}] * len(texts)
        try:
            return cls._pipeline(texts, batch_size=8)
        except Exception:
            logger.exception("FinBERT inference failed")
            return [{"label": "neutral", "score": 0.0}] * len(texts)


# ── Public interface ───────────────────────────────────────────────────────

def score_articles(articles: list[ArticleResult]) -> Optional[SentimentResult]:
    if not articles:
        return None

    texts = [a.raw_text[:512] for a in articles]
    raw_scores = SentimentModel.score(texts)

    scored: list[ScoredArticle] = []
    for article, result in zip(articles, raw_scores):
        label_key = result["label"].lower()
        sentiment, color, icon = _LABEL_MAP.get(label_key, ("Neutral", "yellow", "●"))
        numeric = _SCORE_MAP.get(label_key, 0.0)
        # Scale by confidence
        signed_score = round(numeric * float(result.get("score", 1.0)), 3)
        scored.append(
            ScoredArticle(
                title=article.title,
                source=article.source,
                published=article.published,
                sentiment=sentiment,
                color=color,
                icon=icon,
                score=signed_score,
            )
        )

    avg_score = sum(a.score for a in scored) / len(scored)
    bullish = sum(1 for a in scored if a.sentiment == "Bullish")
    bearish = sum(1 for a in scored if a.sentiment == "Bearish")
    neutral = sum(1 for a in scored if a.sentiment == "Neutral")

    if avg_score >= 0.15:
        agg_label, agg_color = "Strongly Bullish", "bold green"
    elif avg_score >= 0.05:
        agg_label, agg_color = "Bullish", "green"
    elif avg_score <= -0.15:
        agg_label, agg_color = "Strongly Bearish", "bold red"
    elif avg_score <= -0.05:
        agg_label, agg_color = "Bearish", "red"
    else:
        agg_label, agg_color = "Neutral", "yellow"

    return SentimentResult(
        label=agg_label,
        score=round(avg_score, 3),
        color=agg_color,
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
        articles=tuple(scored),
    )
