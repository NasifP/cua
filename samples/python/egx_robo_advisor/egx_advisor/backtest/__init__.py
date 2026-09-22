"""Backtesting: measuring frictions, not discovering parameters.

See `types.py` for why that distinction is enforced rather than merely stated,
and docs/NO_OVERFIT_CHARTER.md for the rules a change to the strategy must meet.
"""

from .data import HistoryError, load_csv, synthetic_history, SYNTHETIC_WARNING
from .engine import BacktestConfig, run_backtest, run_buy_and_hold
from .fills import FillModel
from .metrics import Comparison, Metrics, compare, compute, sample_size_warning
from .types import Bar, BacktestResult, CostModel, DaySnapshot, Fill, MacroBar, PriceHistory

__all__ = [
    "Bar", "BacktestConfig", "BacktestResult", "Comparison", "CostModel", "DaySnapshot",
    "Fill", "FillModel", "HistoryError", "MacroBar", "Metrics", "PriceHistory",
    "SYNTHETIC_WARNING", "compare", "compute", "load_csv", "run_backtest",
    "run_buy_and_hold", "sample_size_warning", "synthetic_history",
]
