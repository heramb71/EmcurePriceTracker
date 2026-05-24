import pytest

from emcure_tracker.data.news import ArticleResult
from emcure_tracker.sentiment import SentimentModel, score_articles


def _articles(n: int = 3) -> list[ArticleResult]:
    return [
        ArticleResult(
            title=f"Emcure reports strong Q{i} earnings",
            source="Test",
            published="2024-01-01",
            raw_text=f"Emcure Pharmaceuticals Q{i} results beat expectations.",
        )
        for i in range(1, n + 1)
    ]


def test_score_articles_returns_none_on_empty():
    result = score_articles([])
    assert result is None


def test_score_articles_without_model():
    # Model not loaded in unit tests — SentimentModel._pipeline is None,
    # which triggers neutral fallback scores
    SentimentModel._pipeline = None
    result = score_articles(_articles(3))
    assert result is not None
    assert result.bullish + result.bearish + result.neutral == 3
    assert -1.0 <= result.score <= 1.0


def test_sentiment_result_fields():
    SentimentModel._pipeline = None
    result = score_articles(_articles(5))
    assert result is not None
    assert len(result.articles) == 5
    assert result.label in {"Strongly Bullish", "Bullish", "Neutral", "Bearish", "Strongly Bearish"}
