"""A daily cap on model spending that holds across every process.

The $5 trajectory budget passed to cua-agent never tripped: it adds up a
`response_cost` the Gemini loop does not set, and it resets on every run. This
ledger sits in front of every model call the bot makes -- reading the
portfolio, classifying news, answering chat -- and keeps one count per Cairo
day on the shared bus, so the agent and the dashboard spend from one budget.

Two limits, because only one of them is always measurable:

- EGX_DAILY_CALL_LIMIT (default 400) counts calls. It always works.
- EGX_DAILY_SPEND_LIMIT_USD (default 2.00) counts dollars, from litellm's own
  price table. A model litellm has no price for adds nothing to the dollar
  total, and the dashboard says how many calls went unpriced, so the call
  limit is the one that cannot be fooled.

Either limit reached refuses the next call with BudgetExceeded. The caller
treats that like any other unavailable model: the classifier falls back to
keywords, the portfolio read is retried next cycle, the chat says so.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from .clock import CAIRO

logger = logging.getLogger(__name__)

SNAPSHOT_KEY = "spend"
DEFAULT_CALL_LIMIT = 400
DEFAULT_USD_LIMIT = 2.00


class BudgetExceeded(RuntimeError):
    """Today's model budget is used up. Not retried until the Cairo day turns."""


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(CAIRO)).astimezone(CAIRO).date().isoformat()


def limits(env: Mapping[str, str] = os.environ) -> tuple[int, float]:
    def number(key: str, default: float) -> float:
        try:
            return float(env.get(key) or default)
        except ValueError:
            return default

    return (
        int(number("EGX_DAILY_CALL_LIMIT", DEFAULT_CALL_LIMIT)),
        number("EGX_DAILY_SPEND_LIMIT_USD", DEFAULT_USD_LIMIT),
    )


def _fresh(day: str) -> dict[str, Any]:
    return {"day": day, "calls": 0, "usd": 0.0, "unpriced_calls": 0, "by_purpose": {}}


def today(bus: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """Today's totals, zeroed when the stored ones belong to another day."""
    day = _today(now)
    stored = (bus.get(SNAPSHOT_KEY) or {}).get("payload") or {}
    return stored if stored.get("day") == day else _fresh(day)


def _cost(response: Any, model: str) -> Optional[float]:
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response, model=model))
    except Exception:  # noqa: BLE001 - unknown model or response shape: unpriced
        return None


def metered(
    completion: Callable[..., Any],
    *,
    purpose: str,
    bus_factory: Optional[Callable[[], Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    cost: Callable[[Any, str], Optional[float]] = _cost,
    now: Callable[[], Optional[datetime]] = lambda: None,
) -> Callable[..., Any]:
    """Wrap a litellm-style `completion` so every call is checked and counted."""

    def open_bus() -> Any:
        if bus_factory is not None:
            return bus_factory()
        from .bus import StateBus
        from .paths import bus_path

        return StateBus(bus_path())

    def call(**kwargs: Any) -> Any:
        call_limit, usd_limit = limits(env if env is not None else os.environ)
        bus = open_bus()
        try:
            current = today(bus, now())
            if current["calls"] >= call_limit:
                raise BudgetExceeded(
                    f"daily model call limit reached ({current['calls']}/{call_limit}); "
                    "raise EGX_DAILY_CALL_LIMIT in Settings to allow more"
                )
            if current["usd"] >= usd_limit:
                raise BudgetExceeded(
                    f"daily model spend limit reached (${current['usd']:.2f}/"
                    f"${usd_limit:.2f}); raise EGX_DAILY_SPEND_LIMIT_USD in Settings"
                )
            response = completion(**kwargs)
            spent = cost(response, str(kwargs.get("model", "")))
            day = _today(now())

            def add(stored: Any) -> dict[str, Any]:
                totals = stored if isinstance(stored, dict) and stored.get("day") == day \
                    else _fresh(day)
                totals["calls"] += 1
                if spent is None:
                    totals["unpriced_calls"] += 1
                else:
                    totals["usd"] = round(totals["usd"] + spent, 6)
                per = totals["by_purpose"].setdefault(purpose, {"calls": 0, "usd": 0.0})
                per["calls"] += 1
                per["usd"] = round(per["usd"] + (spent or 0.0), 6)
                return totals

            bus.update(SNAPSHOT_KEY, add)
            return response
        finally:
            close = getattr(bus, "close", None)
            if close and bus_factory is None:
                close()

    return call


def describe(totals: Mapping[str, Any], env: Mapping[str, str] = os.environ) -> str:
    call_limit, usd_limit = limits(env)
    text = (
        f"today: ${totals.get('usd', 0):.2f} of ${usd_limit:.2f}, "
        f"{totals.get('calls', 0)} of {call_limit} calls"
    )
    if totals.get("unpriced_calls"):
        text += f" ({totals['unpriced_calls']} unpriced)"
    return text
