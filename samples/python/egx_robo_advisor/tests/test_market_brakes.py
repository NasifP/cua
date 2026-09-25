"""Scheduled events and official-source outages stop new buys, and nothing else."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from egx_advisor.regime.events import (
    EventCalendarError,
    ScheduledEvent,
    active_events,
    load_events,
    save_events,
    upcoming,
)
from egx_advisor.regime.filter import RegimeFilter
from egx_advisor.regime.sources import load_feeds, read_feed_entries, save_feeds
from egx_advisor.types import RiskState

NOON = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)  # 12:00 Cairo


def test_a_high_impact_event_covers_the_day_before_and_after() -> None:
    event = ScheduledEvent(date(2026, 11, 20), "CBE MPC", "high")
    for day in (19, 20, 21):
        assert active_events([event], datetime(2026, 11, day, 10, tzinfo=timezone.utc))
    assert not active_events([event], datetime(2026, 11, 22, 10, tzinfo=timezone.utc))


def test_a_medium_event_covers_its_own_day_only() -> None:
    event = ScheduledEvent(date(2026, 11, 20), "CPI", "medium")
    assert active_events([event], NOON)
    assert not active_events([event], datetime(2026, 11, 19, 10, tzinfo=timezone.utc))


def test_events_round_trip_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "events.toml"
    events = [ScheduledEvent(date(2026, 11, 20), 'CBE "MPC"', "high"),
              ScheduledEvent(date(2026, 10, 10), "CPI", "medium")]
    save_events(path, events)
    loaded = load_events(path)
    assert [e.title for e in loaded] == ["CPI", 'CBE "MPC"']
    assert upcoming(loaded, datetime(2026, 10, 1, tzinfo=timezone.utc))[0].title == "CPI"


def test_a_bad_event_file_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "events.toml"
    path.write_text('[[event]]\ndate = 2026-11-20\ntitle = "x"\nimpact = "huge"\n')
    with pytest.raises(EventCalendarError):
        load_events(path)


def test_the_shipped_event_file_guesses_no_dates() -> None:
    root = Path(__file__).resolve().parent.parent
    assert load_events(root / "config" / "events.toml") == ()


def test_a_scheduled_event_halts_buys_and_says_why() -> None:
    state = RegimeFilter().evaluate(
        [], now=NOON, feed_age_seconds=30, scheduled=("CBE MPC (2026-11-20, high)",)
    )
    assert state.risk_state is RiskState.BUYS_HALTED
    assert state.drivers[0].startswith("scheduled event: CBE MPC")


def test_an_official_source_outage_halts_buys_in_session_only() -> None:
    in_session = RegimeFilter().evaluate(
        [], now=NOON, feed_age_seconds=30, sources_failed=("EGX disclosures",),
        authoritative_failed=("EGX disclosures",),
    )
    assert in_session.risk_state is RiskState.BUYS_HALTED
    assert "official source unavailable" in in_session.drivers[0]

    closed = RegimeFilter().evaluate(
        [], now=NOON, feed_age_seconds=30, sources_failed=("EGX disclosures",),
        authoritative_failed=("EGX disclosures",), in_session=False,
    )
    assert closed.risk_state is RiskState.RISK_ON


def test_a_poll_with_failing_sources_is_not_a_clean_poll() -> None:
    regime = RegimeFilter()
    regime.evaluate([], now=NOON, feed_age_seconds=30, sources_failed=("Mubasher",))
    assert regime._clean_polls == 0


def test_feeds_file_is_actually_used(tmp_path: Path) -> None:
    """It existed, but nothing read it, so editing it changed nothing."""
    path = tmp_path / "feeds.toml"
    save_feeds(path, [
        {"name": "Official", "url": "https://x.test/rss", "authoritative": True},
        {"name": "Off", "url": "https://y.test/rss", "enabled": False},
    ])
    sources = load_feeds(path)
    assert [s.name for s in sources] == ["Official"]
    assert sources[0].authoritative
    assert len(read_feed_entries(path)) == 2, "disabled feeds stay in the file"


def test_a_feed_without_a_url_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "feeds.toml"
    path.write_text('[[source]]\nname = "x"\nurl = "file:///etc/passwd"\n')
    with pytest.raises(ValueError, match="http"):
        load_feeds(path)


def test_the_agent_reads_the_configured_feeds() -> None:
    source = (Path(__file__).resolve().parent.parent / "egx_advisor" / "egx_cua_agent.py"
              ).read_text(encoding="utf-8")
    assert 'load_feeds(config_path("feeds.toml"))' in source
