from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import feedparser

from emcure_tracker import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleResult:
    title: str
    source: str
    published: str
    raw_text: str               # title + summary, used for sentiment scoring


def fetch_all() -> Optional[list[ArticleResult]]:
    articles: list[ArticleResult] = []
    query_terms = set(config.NEWS_QUERY.lower().split())

    for url, source_name in config.NEWS_RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[: config.MAX_NEWS_ITEMS]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                pub = entry.get("published", "")

                # For MoneyControl / ET feeds, filter to Emcure-relevant entries
                combined = (title + " " + summary).lower()
                if source_name != "Google News":
                    if not any(t in combined for t in ("emcure", "pharma", "pharmaceutical")):
                        continue

                articles.append(
                    ArticleResult(
                        title=title[:100] + ("…" if len(title) > 100 else ""),
                        source=source_name,
                        published=pub[:25] if pub else "–",
                        raw_text=f"{title}. {summary}",
                    )
                )
        except Exception:
            logger.exception("fetch_all failed for source %s", source_name)

    return articles if articles else None
