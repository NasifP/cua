"""The Chart and Events & news tabs: symbol handling and file round trips."""

from __future__ import annotations

import os
from datetime import date

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

from egx_advisor.desktop import chart_tab  # noqa: E402
from egx_advisor.regime.events import ScheduledEvent, load_events  # noqa: E402
from egx_advisor.regime.sources import load_feeds, read_feed_entries  # noqa: E402


def test_egx_symbols_map_to_the_tradingview_exchange():
    assert chart_tab.tradingview_symbol("COMI.CA") == "EGX:COMI"
    assert chart_tab.tradingview_symbol(" etel.ca ") == "EGX:ETEL"
    assert chart_tab.tradingview_symbol("TVC:GOLD") == "TVC:GOLD"


def test_a_typed_symbol_cannot_end_the_script_block():
    html = chart_tab.widget_html('x"</script><script>alert(1)</script>')
    script = html.split("<script>", 1)[1]
    assert "</script><script>alert" not in script
    assert script.count("</script>") == 1


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_events_added_and_removed_from_the_tab_reach_the_file(app, tmp_path, monkeypatch):
    from egx_advisor.desktop import sources_tab

    events = tmp_path / "events.toml"
    monkeypatch.setattr(sources_tab, "EVENTS_FILE", events)
    monkeypatch.setattr(sources_tab, "FEEDS_FILE", tmp_path / "feeds.toml")
    tab = sources_tab.SourcesTab()

    tab.event_title.setText("CBE MPC rate decision")
    tab.event_impact.setCurrentText("high")
    tab.add_event()
    [event] = load_events(events)
    assert event.title == "CBE MPC rate decision" and event.impact == "high"
    assert event.day == date.today()

    tab.event_title.setText("")
    tab.add_event()  # no title: refused, nothing written
    assert len(load_events(events)) == 1

    tab.events.selectRow(0)
    tab.remove_event()
    assert load_events(events) == ()
    assert isinstance(event, ScheduledEvent)


def test_feeds_keep_the_defaults_and_can_be_disabled(app, tmp_path, monkeypatch):
    from egx_advisor.desktop import sources_tab

    feeds = tmp_path / "feeds.toml"
    monkeypatch.setattr(sources_tab, "EVENTS_FILE", tmp_path / "events.toml")
    monkeypatch.setattr(sources_tab, "FEEDS_FILE", feeds)
    tab = sources_tab.SourcesTab()
    defaults = len(tab._feeds)

    tab.feed_url.setText("ftp://not-a-feed")
    tab.add_feed()
    assert not feeds.exists()

    tab.feed_name.setText("Example")
    tab.feed_url.setText("https://example.com/rss")
    tab.add_feed()
    assert len(read_feed_entries(feeds)) == defaults + 1

    tab.feeds.selectRow(defaults)
    tab.toggle_feed()
    assert "https://example.com/rss" not in {s.url for s in load_feeds(feeds)}
    assert len(read_feed_entries(feeds)) == defaults + 1
