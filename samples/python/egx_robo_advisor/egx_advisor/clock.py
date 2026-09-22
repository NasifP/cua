"""EGX trading calendar.

The Egyptian Exchange runs Sunday through Thursday; Friday and Saturday are the
weekend. The continuous session is 10:00-14:30 Cairo time, and Cairo observes
DST again as of 2023, so we resolve everything through the Africa/Cairo zone
rather than a hard-coded UTC+2.

A rebalancer has no business trading the opening or closing auction, so the loop
only executes inside a narrowed window. Everything here is pure: the caller
supplies `now`, which keeps the calendar testable and lets a backtest replay it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

try:  # pragma: no cover - environment dependent
    from zoneinfo import ZoneInfo

    CAIRO = ZoneInfo("Africa/Cairo")
except Exception:  # pragma: no cover - tzdata missing
    from datetime import timezone

    #: Fallback. Loses DST, so the window can be off by an hour in summer; the
    #: agent logs this at startup instead of silently trading at the wrong time.
    CAIRO = timezone(timedelta(hours=2))
    TZDATA_AVAILABLE = False
else:
    TZDATA_AVAILABLE = True

#: Continuous session, Cairo local time.
SESSION_OPEN = time(10, 0)
SESSION_CLOSE = time(14, 30)

#: We stay out of the auction-adjacent noise at both ends of the session.
OPEN_BUFFER = timedelta(minutes=15)
CLOSE_BUFFER = timedelta(minutes=20)

#: Monday==0 ... Sunday==6. EGX is closed Friday (4) and Saturday (5).
WEEKEND_WEEKDAYS = frozenset({4, 5})

#: EGX settles equities on T+2, so proceeds of a sale are not investable today.
SETTLEMENT_DAYS = 2


class SessionState(enum.Enum):
    CLOSED_WEEKEND = "closed_weekend"
    CLOSED_HOLIDAY = "closed_holiday"
    PRE_OPEN = "pre_open"
    #: Inside the session but within the opening/closing buffer.
    AUCTION_BUFFER = "auction_buffer"
    TRADEABLE = "tradeable"
    POST_CLOSE = "post_close"

    @property
    def can_trade(self) -> bool:
        return self is SessionState.TRADEABLE


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """EGX calendar with an explicit holiday set.

    Egyptian market holidays move with the Hijri calendar and are published
    annually by EGX, so they are data, not a rule we try to compute. An empty
    set is honest about knowing nothing; populate it from config each year.
    """

    holidays: frozenset[date] = frozenset()

    def is_session_day(self, day: date) -> bool:
        return day.weekday() not in WEEKEND_WEEKDAYS and day not in self.holidays

    def state_at(self, now: datetime) -> SessionState:
        local = _to_cairo(now)
        day = local.date()

        if day.weekday() in WEEKEND_WEEKDAYS:
            return SessionState.CLOSED_WEEKEND
        if day in self.holidays:
            return SessionState.CLOSED_HOLIDAY

        open_at = datetime.combine(day, SESSION_OPEN, tzinfo=local.tzinfo)
        close_at = datetime.combine(day, SESSION_CLOSE, tzinfo=local.tzinfo)

        if local < open_at:
            return SessionState.PRE_OPEN
        if local >= close_at:
            return SessionState.POST_CLOSE
        if local < open_at + OPEN_BUFFER or local > close_at - CLOSE_BUFFER:
            return SessionState.AUCTION_BUFFER
        return SessionState.TRADEABLE

    def can_trade(self, now: datetime) -> bool:
        return self.state_at(now).can_trade

    def next_session_open(self, now: datetime) -> datetime:
        """First tradeable instant at or after `now`, in Cairo time."""
        local = _to_cairo(now)
        first_tradeable_time = (
            datetime.combine(local.date(), SESSION_OPEN, tzinfo=local.tzinfo) + OPEN_BUFFER
        ).timetz()

        candidate_day = local.date()
        if not (
            self.is_session_day(candidate_day) and local.timetz() < first_tradeable_time
        ):
            candidate_day += timedelta(days=1)

        for _ in range(365):
            if self.is_session_day(candidate_day):
                return datetime.combine(
                    candidate_day, first_tradeable_time.replace(tzinfo=None), tzinfo=CAIRO
                ) + timedelta(0)
            candidate_day += timedelta(days=1)
        raise RuntimeError("no session day found within a year; check the holiday set")

    def settlement_date(self, trade_day: date) -> date:
        """T+2 in session days, which is when sale proceeds become investable."""
        day = trade_day
        remaining = SETTLEMENT_DAYS
        while remaining > 0:
            day += timedelta(days=1)
            if self.is_session_day(day):
                remaining -= 1
        return day


def _to_cairo(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass an aware datetime")
    return moment.astimezone(CAIRO)
