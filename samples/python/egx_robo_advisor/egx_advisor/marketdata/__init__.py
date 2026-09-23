"""Market data providers.

`MarketDataProvider` is the seam. `JsonFileMarketData` serves development and
backtests; `YahooMarketData` is the live path. Both validate what they return --
see `base.py` for why a provider that cannot vouch for its data raises instead of
degrading.
"""

from .base import (
    DataQualityError,
    Finding,
    MarketDataError,
    MarketDataProvider,
    QualityPolicy,
    QualityReport,
    Severity,
)
from .jsonfile import JsonFileMarketData
from .quality import validate_bars, validate_quotes
from .yahoo import YahooMarketData, YahooRow

__all__ = [
    "DataQualityError",
    "Finding",
    "JsonFileMarketData",
    "MarketDataError",
    "MarketDataProvider",
    "QualityPolicy",
    "QualityReport",
    "Severity",
    "YahooMarketData",
    "YahooRow",
    "validate_bars",
    "validate_quotes",
]
