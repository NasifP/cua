"""Regime layer: news in, permission-to-buy out. Never a trading signal."""

from .filter import (
    FilterConfig,
    RegimeFilter,
    SubtractiveInvariantViolation,
)
from .sentiment import (
    POSITIVE_SENTIMENT_IS_IGNORED,
    KeywordClassifier,
    LlmClassifier,
    combine,
)
from .sources import DEFAULT_SOURCES, FeedPollResult, FeedSource, NewsFetcher, parse_feed

__all__ = [
    "DEFAULT_SOURCES",
    "FeedPollResult",
    "FeedSource",
    "FilterConfig",
    "KeywordClassifier",
    "LlmClassifier",
    "NewsFetcher",
    "POSITIVE_SENTIMENT_IS_IGNORED",
    "RegimeFilter",
    "SubtractiveInvariantViolation",
    "combine",
    "parse_feed",
]
