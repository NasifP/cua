"""EGX calendar: Sunday-Thursday sessions, Cairo time, T+2 settlement."""

from datetime import date, datetime, timedelta

import pytest

from egx_advisor.clock import CAIRO, SessionState, TradingCalendar


def cairo(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=CAIRO)


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar()


@pytest.mark.parametrize(
    "moment,expected",
    [
        ("2026-09-20T11:00", SessionState.TRADEABLE),       # Sunday
        ("2026-09-24T11:00", SessionState.TRADEABLE),       # Thursday
        ("2026-09-25T11:00", SessionState.CLOSED_WEEKEND),  # Friday
        ("2026-09-26T11:00", SessionState.CLOSED_WEEKEND),  # Saturday
        ("2026-09-22T09:00", SessionState.PRE_OPEN),
        ("2026-09-22T10:05", SessionState.AUCTION_BUFFER),  # opening buffer
        ("2026-09-22T14:20", SessionState.AUCTION_BUFFER),  # closing buffer
        ("2026-09-22T15:00", SessionState.POST_CLOSE),
    ],
)
def test_session_states(calendar: TradingCalendar, moment: str, expected: SessionState) -> None:
    assert calendar.state_at(cairo(moment)) is expected


def test_only_tradeable_permits_trading(calendar: TradingCalendar) -> None:
    assert calendar.can_trade(cairo("2026-09-22T11:00"))
    # The auction buffers are inside the session but deliberately not tradeable.
    assert not calendar.can_trade(cairo("2026-09-22T10:05"))


def test_holidays_close_the_market() -> None:
    calendar = TradingCalendar(holidays=frozenset({date(2026, 9, 22)}))
    assert calendar.state_at(cairo("2026-09-22T11:00")) is SessionState.CLOSED_HOLIDAY
    assert not calendar.can_trade(cairo("2026-09-22T11:00"))


def test_next_session_open_skips_the_weekend(calendar: TradingCalendar) -> None:
    # From Friday, the next session is Sunday.
    next_open = calendar.next_session_open(cairo("2026-09-25T11:00"))
    assert next_open.date() == date(2026, 9, 27)
    assert calendar.can_trade(next_open)


def test_next_session_open_is_today_if_still_ahead(calendar: TradingCalendar) -> None:
    next_open = calendar.next_session_open(cairo("2026-09-22T08:00"))
    assert next_open.date() == date(2026, 9, 22)


def test_next_session_open_rolls_over_after_the_open(calendar: TradingCalendar) -> None:
    next_open = calendar.next_session_open(cairo("2026-09-22T13:00"))
    assert next_open.date() == date(2026, 9, 23)


def test_settlement_counts_session_days_only(calendar: TradingCalendar) -> None:
    # Thursday + 2 session days lands on Monday, skipping Fri/Sat.
    assert calendar.settlement_date(date(2026, 9, 24)) == date(2026, 9, 28)
    assert calendar.settlement_date(date(2026, 9, 20)) == date(2026, 9, 22)


def test_naive_datetime_is_rejected(calendar: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="naive"):
        calendar.state_at(datetime(2026, 9, 22, 11, 0))
