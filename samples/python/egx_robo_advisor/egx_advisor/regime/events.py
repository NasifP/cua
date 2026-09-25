"""Scheduled market events as a brake: no new buys around them.

Central-bank rate decisions and inflation prints move the whole EGX, and they
are announced in advance. Buying the day before one is a bet on the outcome,
which this bot does not make. So on the days around a listed event the regime
goes to BUYS_HALTED: sells still run, nothing new is bought.

The list is config/events.toml, kept by the operator from the Central Bank of
Egypt's published meeting schedule, CAPMAS release dates, or an economic
calendar such as Investing.com's. Nothing here fetches it: calendar sites'
terms generally forbid automated collection, and a scraped calendar that
silently stops updating is worse than a short list the operator can see.

Like every regime input, this can only subtract.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from ..clock import CAIRO

IMPACTS = ("high", "medium")


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    day: date
    title: str
    #: "high" halts buys the session before, of, and after; "medium" only the day of.
    impact: str = "high"

    def window(self) -> tuple[date, date]:
        pad = 1 if self.impact == "high" else 0
        return self.day - timedelta(days=pad), self.day + timedelta(days=pad)


class EventCalendarError(ValueError):
    """config/events.toml could not be read."""


def load_events(path: Path) -> tuple[ScheduledEvent, ...]:
    if not path.exists():
        return ()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise EventCalendarError(f"{path}: {exc}") from exc
    events = []
    for entry in raw.get("event", []):
        try:
            day = entry["date"]
            if isinstance(day, str):
                day = date.fromisoformat(day)
            impact = str(entry.get("impact", "high")).lower()
            if impact not in IMPACTS:
                raise ValueError(f"impact must be one of {IMPACTS}")
            title = str(entry["title"]).strip()
            if not title:
                raise ValueError("title is empty")
            events.append(ScheduledEvent(day, title, impact))
        except (KeyError, TypeError, ValueError) as exc:
            raise EventCalendarError(f"{path}: bad event {entry!r}: {exc}") from exc
    return tuple(sorted(events, key=lambda e: e.day))


def save_events(path: Path, events: Iterable[ScheduledEvent]) -> None:
    lines = [
        "# Scheduled events that stop new buys around them (see egx_advisor/regime/events.py).",
        "# Edited from the desktop app's Settings tab, or by hand:",
        '#   [[event]]  date = 2026-11-20  title = "CBE MPC rate decision"  impact = "high"',
        "# high: no buys the day before, the day of, and the day after. medium: day of only.",
        "",
    ]
    for event in sorted(events, key=lambda e: e.day):
        title = event.title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines += [
            "[[event]]",
            f"date = {event.day.isoformat()}",
            f'title = "{title}"',
            f'impact = "{event.impact}"',
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def active_events(
    events: Sequence[ScheduledEvent], now: Optional[datetime] = None
) -> tuple[ScheduledEvent, ...]:
    """Events whose window covers today's Cairo date."""
    today = (now or datetime.now(timezone.utc)).astimezone(CAIRO).date()
    return tuple(e for e in events if e.window()[0] <= today <= e.window()[1])


def upcoming(
    events: Sequence[ScheduledEvent], now: Optional[datetime] = None, days: int = 30
) -> tuple[ScheduledEvent, ...]:
    today = (now or datetime.now(timezone.utc)).astimezone(CAIRO).date()
    return tuple(e for e in events if today <= e.day <= today + timedelta(days=days))
