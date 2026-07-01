"""
Type-safe data structures for core trading domain objects.
Uses dataclasses for runtime validation and IDE autocomplete support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Quote:
    """Current market quote for a security."""

    price: float
    open: float
    high: float
    low: float
    close: float
    volume: int
    prev_close: float
    change: float
    change_pct: float
    date: str
    source: str = "daily"  # "daily" or "live"

    @classmethod
    def from_dict(cls, d: dict) -> Quote:
        """Safely construct from dict with defaults."""
        return cls(
            price=float(d.get("price", 0.0)),
            open=float(d.get("open", 0.0)),
            high=float(d.get("high", 0.0)),
            low=float(d.get("low", 0.0)),
            close=float(d.get("close", 0.0)),
            volume=int(d.get("volume", 0)),
            prev_close=float(d.get("prev_close", 0.0)),
            change=float(d.get("change", 0.0)),
            change_pct=float(d.get("change_pct", 0.0)),
            date=str(d.get("date", "")),
            source=str(d.get("source", "daily")),
        )

    def to_dict(self) -> dict:
        """Convert to dict for backward compatibility."""
        return {
            "price": self.price,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "prev_close": self.prev_close,
            "change": self.change,
            "change_pct": self.change_pct,
            "date": self.date,
            "source": self.source,
        }


@dataclass
class Article:
    """A single news article with sentiment."""

    title: str
    published: str
    sentiment: str  # "Bullish", "Neutral", "Bearish"
    score: float  # -1.0 to +1.0
    color: str  # "green", "yellow", "red"
    icon: str  # "▲", "●", "▼"


@dataclass
class SentimentResult:
    """Aggregated sentiment from multiple articles."""

    label: (
        str  # "Strongly Bullish", "Bullish", "Neutral", "Bearish", "Strongly Bearish"
    )
    score: float  # average compound score, -1.0 to +1.0
    color: str  # Rich color spec: "bold green", "green", "yellow", "red", "bold red"
    bullish: int  # count of bullish articles
    bearish: int  # count of bearish articles
    neutral: int  # count of neutral articles
    articles: list[Article] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> SentimentResult:
        """Safely construct from dict with defaults."""
        articles = [
            Article(
                title=a.get("title", ""),
                published=a.get("published", ""),
                sentiment=a.get("sentiment", "Neutral"),
                score=float(a.get("score", 0.0)),
                color=a.get("color", "yellow"),
                icon=a.get("icon", "●"),
            )
            for a in d.get("articles", [])
        ]
        return cls(
            label=str(d.get("label", "Neutral")),
            score=float(d.get("score", 0.0)),
            color=str(d.get("color", "yellow")),
            bullish=int(d.get("bullish", 0)),
            bearish=int(d.get("bearish", 0)),
            neutral=int(d.get("neutral", 0)),
            articles=articles,
        )

    def to_dict(self) -> dict:
        """Convert to dict for backward compatibility."""
        return {
            "label": self.label,
            "score": self.score,
            "color": self.color,
            "bullish": self.bullish,
            "bearish": self.bearish,
            "neutral": self.neutral,
            "articles": [
                {
                    "title": a.title,
                    "published": a.published,
                    "sentiment": a.sentiment,
                    "score": a.score,
                    "color": a.color,
                    "icon": a.icon,
                }
                for a in self.articles
            ],
        }


@dataclass
class Indicators:
    """All technical indicators computed from OHLCV."""

    rsi: float  # 0-100
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    ema20: float
    ema50: float
    atr: float
    vwap: float
    avg_volume: int

    @classmethod
    def from_dict(cls, d: dict) -> Indicators:
        """Safely construct from dict with defaults."""
        return cls(
            rsi=float(d.get("rsi", 50.0)),
            macd=float(d.get("macd", 0.0)),
            macd_signal=float(d.get("macd_signal", 0.0)),
            macd_hist=float(d.get("macd_hist", 0.0)),
            bb_upper=float(d.get("bb_upper", 0.0)),
            bb_mid=float(d.get("bb_mid", 0.0)),
            bb_lower=float(d.get("bb_lower", 0.0)),
            ema20=float(d.get("ema20", 0.0)),
            ema50=float(d.get("ema50", 0.0)),
            atr=float(d.get("atr", 0.0)),
            vwap=float(d.get("vwap", 0.0)),
            avg_volume=int(d.get("avg_volume", 0)),
        )

    def to_dict(self) -> dict:
        """Convert to dict for backward compatibility."""
        return {
            "rsi": self.rsi,
            "macd": self.macd,
            "macd_signal": self.macd_signal,
            "macd_hist": self.macd_hist,
            "bb_upper": self.bb_upper,
            "bb_mid": self.bb_mid,
            "bb_lower": self.bb_lower,
            "ema20": self.ema20,
            "ema50": self.ema50,
            "atr": self.atr,
            "vwap": self.vwap,
            "avg_volume": self.avg_volume,
        }


@dataclass
class ScoreBreakdown:
    """Per-signal sub-score contribution (0.0 to 1.0)."""

    pivot: float
    rsi: float
    macd: float
    vwap: float
    sentiment: float
    volume: float

    @classmethod
    def from_dict(cls, d: dict) -> ScoreBreakdown:
        """Safely construct from dict with defaults."""
        return cls(
            pivot=float(d.get("pivot", 0.5)),
            rsi=float(d.get("rsi", 0.5)),
            macd=float(d.get("macd", 0.5)),
            vwap=float(d.get("vwap", 0.5)),
            sentiment=float(d.get("sentiment", 0.5)),
            volume=float(d.get("volume", 0.5)),
        )

    def to_dict(self) -> dict:
        """Convert to dict for backward compatibility."""
        return {
            "pivot": self.pivot,
            "rsi": self.rsi,
            "macd": self.macd,
            "vwap": self.vwap,
            "sentiment": self.sentiment,
            "volume": self.volume,
        }


@dataclass
class ScoreResult:
    """Final trading signal and recommendation."""

    score: float  # 0.0 to 1.0
    signal: str  # "Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"
    signal_color: (
        str  # Rich color spec: "bold green", "green", "yellow", "red", "bold red"
    )
    entry: float  # Entry price (from ATR levels)
    sl: float  # Stop loss
    t1: float  # Target 1
    t2: float  # Target 2
    regime: str  # "Trending Up", "Trending Down", "Ranging", "Unknown"
    breakdown: ScoreBreakdown  # Per-signal contribution

    @classmethod
    def from_dict(cls, d: dict) -> ScoreResult:
        """Safely construct from dict with defaults."""
        breakdown_dict = d.get("breakdown", {})
        breakdown = (
            ScoreBreakdown.from_dict(breakdown_dict)
            if breakdown_dict
            else ScoreBreakdown(
                pivot=0.5, rsi=0.5, macd=0.5, vwap=0.5, sentiment=0.5, volume=0.5
            )
        )
        return cls(
            score=float(d.get("score", 0.5)),
            signal=str(d.get("signal", "Hold")),
            signal_color=str(d.get("signal_color", "yellow")),
            entry=float(d.get("entry", 0.0)),
            sl=float(d.get("sl", 0.0)),
            t1=float(d.get("t1", 0.0)),
            t2=float(d.get("t2", 0.0)),
            regime=str(d.get("regime", "Unknown")),
            breakdown=breakdown,
        )

    def to_dict(self) -> dict:
        """Convert to dict for backward compatibility."""
        return {
            "score": self.score,
            "signal": self.signal,
            "signal_color": self.signal_color,
            "entry": self.entry,
            "sl": self.sl,
            "t1": self.t1,
            "t2": self.t2,
            "regime": self.regime,
            "breakdown": self.breakdown.to_dict(),
        }
