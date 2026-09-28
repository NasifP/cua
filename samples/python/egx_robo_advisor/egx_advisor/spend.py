"""A daily cap on model spending that holds across every process.

The $5 trajectory budget passed to cua-agent never tripped: it adds up a
`response_cost` the Gemini loop does not set, and it resets on every run. This
ledger sits in front of every model call the bot makes -- reading the
portfolio, classifying news, answering chat -- and keeps one count per Cairo
day on the shared bus, so the agent and the dashboard spend from one budget.

Two limits, because only one of them is always measurable:

- EGX_DAILY_CALL_LIMIT (default 400) counts calls. It always works.
- EGX_DAILY_SPEND_LIMIT_EGP (default 100) caps spending in Egyptian pounds.
  Providers bill in dollars, from litellm's own price table, so the ledger
  counts dollars and converts at the day's USD/EGP rate (the agent records it
  as the "fx" snapshot; FALLBACK_USD_EGP until it has). A model litellm has
  no price for adds nothing, and the app says how many calls went unpriced,
  so the call limit is the one that cannot be fooled. The older
  EGX_DAILY_SPEND_LIMIT_USD still works when the EGP limit is not set.

Either limit reached refuses the next call with BudgetExceeded. The caller
treats that like any other unavailable model: the classifier falls back to
keywords, the portfolio read is retried next cycle, the chat says so.

Reservations: a checked call is refused once the limit is reached, but a run
of several calls in parallel (the analyst committee, four model calls) could
all pass the check together and overshoot. Such a run first `reserve`s its
worst case -- the most it can cost, from its bounded rounds and tokens and
the model's price -- in one atomic step, and is refused up front if that does
not fit beside today's spending and every other live reservation. Its calls
then spend from the reservation (`metered(..., reservation=...)`), and
`release` returns what was not used. Every other call counts live
reservations as spent, so nothing can use headroom a run has reserved. A
reservation left behind by a crash expires after RESERVATION_TTL.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from .clock import CAIRO

logger = logging.getLogger(__name__)

SNAPSHOT_KEY = "spend"
DEFAULT_CALL_LIMIT = 400
DEFAULT_USD_LIMIT = 2.00
DEFAULT_EGP_LIMIT = 100.0
#: Used only until the agent has fetched the day's rate.
FALLBACK_USD_EGP = 50.0
FX_KEY = "fx"
#: A reservation not released by then is dropped (the run died).
RESERVATION_TTL = timedelta(minutes=10)


class BudgetExceeded(RuntimeError):
    """Today's model budget is used up. Not retried until the Cairo day turns."""


def _today(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(CAIRO)).astimezone(CAIRO).date().isoformat()


def _number(env: Mapping[str, str], key: str, default: Optional[float]) -> Optional[float]:
    try:
        return float(env[key]) if env.get(key) else default
    except ValueError:
        return default


def usd_egp(bus: Any) -> tuple[float, bool]:
    """The day's USD/EGP rate from the agent's market data, and whether it is one.

    (FALLBACK_USD_EGP, False) until the agent has recorded a rate.
    """
    try:
        rate = float(((bus.get(FX_KEY) or {}).get("payload") or {}).get("usd_egp") or 0)
    except (TypeError, ValueError, AttributeError):
        rate = 0.0
    return (rate, True) if rate > 0 else (FALLBACK_USD_EGP, False)


def limits(env: Mapping[str, str] = os.environ,
           rate: float = FALLBACK_USD_EGP) -> tuple[int, float]:
    """(call limit, spend limit in dollars at `rate` EGP per dollar)."""
    calls = int(_number(env, "EGX_DAILY_CALL_LIMIT", DEFAULT_CALL_LIMIT) or DEFAULT_CALL_LIMIT)
    egp = _number(env, "EGX_DAILY_SPEND_LIMIT_EGP", None)
    if egp is not None:
        return calls, egp / rate
    usd = _number(env, "EGX_DAILY_SPEND_LIMIT_USD", None)
    return calls, usd if usd is not None else DEFAULT_EGP_LIMIT / rate


def in_egp(totals: Mapping[str, Any], env: Mapping[str, str] = os.environ,
           rate: float = FALLBACK_USD_EGP) -> tuple[float, float]:
    """(spent today, daily limit), both in Egyptian pounds."""
    _calls, usd_limit = limits(env, rate)
    return float(totals.get("usd", 0)) * rate, usd_limit * rate


def _fresh(day: str) -> dict[str, Any]:
    return {"day": day, "calls": 0, "usd": 0.0, "unpriced_calls": 0, "by_purpose": {},
            "reservations": {}}


def _now(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(CAIRO)).astimezone(CAIRO)


def _live(totals: dict[str, Any], now: Optional[datetime]) -> dict[str, Any]:
    """Drop expired reservations in place; returns the live ones."""
    moment = _now(now).isoformat()
    live = {k: v for k, v in (totals.get("reservations") or {}).items()
            if str(v.get("expires", "")) > moment}
    totals["reservations"] = live
    return live


def reserved(totals: dict[str, Any], now: Optional[datetime] = None,
             exclude: str = "") -> tuple[float, int]:
    """(dollars, calls) held by live reservations, except `exclude`'s."""
    live = _live(dict(totals), now)
    return (sum(float(v.get("usd", 0)) for k, v in live.items() if k != exclude),
            sum(int(v.get("calls", 0)) for k, v in live.items() if k != exclude))


def today(bus: Any, now: Optional[datetime] = None) -> dict[str, Any]:
    """Today's totals, zeroed when the stored ones belong to another day."""
    day = _today(now)
    stored = (bus.get(SNAPSHOT_KEY) or {}).get("payload") or {}
    totals = stored if stored.get("day") == day else _fresh(day)
    _live(totals, now)
    return totals


# --------------------------------------------------------------------------- #
# Reservations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Reservation:
    id: str
    usd: float
    calls: int
    purpose: str


def price(model: str) -> Optional[tuple[float, float]]:
    """(dollars per input token, per output token) from litellm's table; None if unknown."""
    try:
        import litellm

        info = litellm.model_cost.get(model) or litellm.model_cost.get(model.split("/")[-1])
        if not info:
            info = litellm.get_model_info(model)
        cost_in = float(info.get("input_cost_per_token") or 0)
        cost_out = float(info.get("output_cost_per_token") or 0)
    except Exception:  # noqa: BLE001 - unknown model: unpriced
        return None
    return (cost_in, cost_out) if cost_in or cost_out else None


def reserve(bus: Any, *, usd: float, calls: int, purpose: str,
            env: Optional[Mapping[str, str]] = None,
            now: Callable[[], Optional[datetime]] = lambda: None) -> Reservation:
    """Hold `usd` and `calls` of today's budget for one run, or raise BudgetExceeded.

    Atomic across processes (StateBus.update holds the write lock).
    """
    env = env if env is not None else os.environ
    rate, _ = usd_egp(bus)
    call_limit, usd_limit = limits(env, rate)
    ident = secrets.token_hex(6)
    day = _today(now())
    refusal: list[str] = []

    def hold(stored: Any) -> dict[str, Any]:
        totals = stored if isinstance(stored, dict) and stored.get("day") == day \
            else _fresh(day)
        live = _live(totals, now())
        held_usd = sum(float(v.get("usd", 0)) for v in live.values())
        held_calls = sum(int(v.get("calls", 0)) for v in live.values())
        if totals["calls"] + held_calls + calls > call_limit:
            refusal.append(
                f"this answer needs up to {calls} model calls and only "
                f"{max(call_limit - totals['calls'] - held_calls, 0)} of today's "
                f"{call_limit} are left; raise EGX_DAILY_CALL_LIMIT in Settings")
            return totals
        if totals["usd"] + held_usd + usd > usd_limit + 1e-12:
            left = max(usd_limit - totals["usd"] - held_usd, 0.0)
            refusal.append(
                f"this answer may cost up to {usd * rate:.2f} EGP and only "
                f"{left * rate:.2f} of today's {usd_limit * rate:.2f} EGP are left; "
                "raise the daily model spend limit in Settings")
            return totals
        live[ident] = {"usd": round(usd, 6), "calls": calls, "purpose": purpose,
                       "expires": (_now(now()) + RESERVATION_TTL).isoformat()}
        return totals

    bus.update(SNAPSHOT_KEY, hold)
    if refusal:
        raise BudgetExceeded(refusal[0])
    return Reservation(ident, usd, calls, purpose)


def release(bus: Any, reservation: Reservation,
            now: Callable[[], Optional[datetime]] = lambda: None) -> None:
    """Give back what a run did not use. Its spending stays counted."""
    day = _today(now())

    def drop(stored: Any) -> Any:
        if isinstance(stored, dict) and stored.get("day") == day:
            (stored.get("reservations") or {}).pop(reservation.id, None)
        return stored

    bus.update(SNAPSHOT_KEY, drop)


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
    reservation: Optional[Reservation] = None,
    on_cost: Callable[[Optional[float]], None] = lambda spent: None,
) -> Callable[..., Any]:
    """Wrap a litellm-style `completion` so every call is checked and counted.

    With a `reservation`, a call spends from it: the run's own reservation is
    left out of what is held, so its reserved headroom is usable, and its cost
    comes off the reservation as it is added to the day. Both limits are still
    checked on every call: a run whose real spending passes its estimate (or
    that reserved no dollars because its model has no price) is refused once
    the day is spent, like any other call. Without a reservation, live
    reservations count as spent.
    `on_cost` gets each call's dollars (None when unpriced), for a run's total.
    """

    def open_bus() -> Any:
        if bus_factory is not None:
            return bus_factory()
        from .bus import StateBus
        from .paths import bus_path

        return StateBus(bus_path())

    def call(**kwargs: Any) -> Any:
        bus = open_bus()
        try:
            rate, _measured = usd_egp(bus)
            call_limit, usd_limit = limits(env if env is not None else os.environ, rate)
            current = today(bus, now())
            own = (current.get("reservations") or {}).get(reservation.id) \
                if reservation is not None else None
            # The run's own reservation is excluded, never skipped past: the
            # checks below hold for reserved calls too.
            held_usd, held_calls = reserved(current, now(),
                                            exclude=reservation.id if own else "")
            if current["calls"] + held_calls >= call_limit:
                raise BudgetExceeded(
                    f"daily model call limit reached ({current['calls']}/{call_limit}); "
                    "raise EGX_DAILY_CALL_LIMIT in Settings to allow more"
                )
            if current["usd"] + held_usd >= usd_limit:
                raise BudgetExceeded(
                    f"daily model spend limit reached ({current['usd'] * rate:.2f}/"
                    f"{usd_limit * rate:.2f} EGP); raise the daily model spend limit "
                    f"in Settings"
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
                held = (totals.setdefault("reservations", {}).get(reservation.id)
                        if reservation is not None else None)
                if held is not None:
                    # What was reserved is now spent: move it, don't count it twice.
                    held["calls"] = max(int(held.get("calls", 0)) - 1, 0)
                    held["usd"] = round(max(float(held.get("usd", 0)) - (spent or 0.0), 0.0), 6)
                return totals

            bus.update(SNAPSHOT_KEY, add)
            on_cost(spent)
            return response
        finally:
            close = getattr(bus, "close", None)
            if close and bus_factory is None:
                close()

    return call


def describe(totals: Mapping[str, Any], env: Mapping[str, str] = os.environ,
             rate: float = FALLBACK_USD_EGP) -> str:
    call_limit, _ = limits(env, rate)
    spent, limit = in_egp(totals, env, rate)
    text = (
        f"today: {spent:.2f} of {limit:.2f} EGP (${totals.get('usd', 0):.2f}), "
        f"{totals.get('calls', 0)} of {call_limit} calls"
    )
    if totals.get("unpriced_calls"):
        text += f" ({totals['unpriced_calls']} unpriced)"
    return text
