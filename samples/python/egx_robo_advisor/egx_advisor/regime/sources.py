"""News acquisition for the regime filter.

Scope discipline
----------------
This module fetches public RSS/Atom feeds and official disclosure feeds. It does
not log in, does not defeat paywalls, and does not scrape pages that a site's
robots.txt disallows. That is partly an ethics and terms-of-service matter and
partly engineering: a fragile HTML scraper that breaks silently would feed the
circuit breaker stale data, and a circuit breaker that silently stops seeing bad
news is worse than no circuit breaker at all. Confirm each publisher's terms
before pointing the bot at it, and prefer official EGX disclosure feeds as the
authoritative source for halts and suspensions.

Robustness properties that matter for a safety component:

  * **Per-source isolation.** One dead feed degrades coverage; it never takes the
    poll down. The caller is told which sources failed so it can decide whether
    coverage is thin enough to warrant standing down.
  * **Conditional GET.** ETag / If-Modified-Since, so polling every few minutes
    is cheap and polite.
  * **Bounded everything.** Response size, redirect count, and timeout are all
    capped, because a hung fetch inside a trading loop is a stuck bot.
  * **Feeds are untrusted input.** Headlines are data, never instructions. They
    are classified and length-clamped, never executed, never interpolated into a
    prompt without delimiting, and never used to construct an order.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from ..types import Headline

logger = logging.getLogger(__name__)

USER_AGENT = "egx-robo-advisor/1.0 (personal portfolio bot; contact via repo)"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TITLE_CHARS = 400
DEFAULT_TIMEOUT = 12.0


@dataclass(frozen=True, slots=True)
class FeedSource:
    """One feed. `authoritative` marks exchange-official disclosure channels."""

    name: str
    url: str
    #: Official EGX/regulator feeds are trusted for halts; press is not.
    authoritative: bool = False
    #: Minimum seconds between polls of this source.
    min_interval: float = 120.0


#: Starting set. Verify each URL and each publisher's terms of use before arming;
#: feed paths change, and an authoritative-looking URL that 404s quietly is a
#: coverage hole in the circuit breaker.
DEFAULT_SOURCES: tuple[FeedSource, ...] = (
    FeedSource("EGX disclosures", "https://www.egx.com.eg/en/rss/news.aspx", authoritative=True),
    FeedSource("Mubasher Egypt", "https://www.mubasher.info/rss/news?market=EGX"),
    FeedSource("Enterprise MEA", "https://enterprise.press/feed/"),
)


@dataclass
class FetchState:
    """Per-source caching and backoff bookkeeping."""

    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_polled: Optional[datetime] = None
    consecutive_failures: int = 0
    last_error: str = ""


@dataclass
class FeedPollResult:
    headlines: tuple[Headline, ...] = ()
    #: Sources that failed this round, with the reason.
    failures: tuple[tuple[str, str], ...] = ()
    #: Sources that returned 304 or were skipped by rate limiting.
    unchanged: tuple[str, ...] = ()
    polled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def authoritative_ok(self) -> bool:
        return not any(name for name, _ in self.failures if "EGX" in name)


class NewsFetcher:
    """Polls a set of feeds and returns normalised headlines."""

    def __init__(
        self,
        sources: Sequence[FeedSource] = DEFAULT_SOURCES,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_age: timedelta = timedelta(hours=24),
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        self.sources = tuple(sources)
        self.timeout = timeout
        self.max_age = max_age
        self._state: dict[str, FetchState] = {s.name: FetchState() for s in self.sources}
        self._opener = opener or urllib.request.build_opener()

    async def poll(self, *, now: Optional[datetime] = None) -> FeedPollResult:
        """Fetch every due source concurrently. Never raises."""
        now = now or datetime.now(timezone.utc)
        tasks = [asyncio.create_task(self._poll_one(source, now)) for source in self.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        headlines: list[Headline] = []
        failures: list[tuple[str, str]] = []
        unchanged: list[str] = []

        # gather() preserves order and length; strict=True makes that assumption
        # explicit rather than silently truncating if it ever stops holding.
        for source, outcome in zip(self.sources, results, strict=True):
            if isinstance(outcome, BaseException):
                failures.append((source.name, f"{type(outcome).__name__}: {outcome}"))
                continue
            status, payload = outcome
            if status == "ok":
                headlines.extend(payload)
            elif status == "unchanged":
                unchanged.append(source.name)
            else:
                failures.append((source.name, str(payload)))

        return FeedPollResult(
            headlines=tuple(_dedupe(_recent(headlines, now, self.max_age))),
            failures=tuple(failures),
            unchanged=tuple(unchanged),
            polled_at=now,
        )

    async def _poll_one(self, source: FeedSource, now: datetime):
        state = self._state[source.name]
        if state.last_polled and (now - state.last_polled).total_seconds() < source.min_interval:
            return "unchanged", "rate limited"

        # Exponential backoff on a sick source, so we stop hammering it and the
        # loop stops paying the timeout on every cycle.
        if state.consecutive_failures >= 3:
            backoff = min(900.0, source.min_interval * (2 ** (state.consecutive_failures - 2)))
            if state.last_polled and (now - state.last_polled).total_seconds() < backoff:
                return "error", f"backing off after {state.consecutive_failures} failures"

        try:
            body, etag, last_modified, not_modified = await asyncio.to_thread(
                self._fetch, source, state
            )
        except Exception as exc:  # noqa: BLE001
            state.consecutive_failures += 1
            state.last_polled = now
            state.last_error = str(exc)
            logger.warning("feed %s failed: %s", source.name, exc)
            return "error", str(exc)

        state.last_polled = now
        state.consecutive_failures = 0
        state.last_error = ""

        if not_modified:
            return "unchanged", "304"

        state.etag = etag or state.etag
        state.last_modified = last_modified or state.last_modified

        try:
            return "ok", parse_feed(body, source.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("feed %s did not parse: %s", source.name, exc)
            return "error", f"parse failed: {exc}"

    def _fetch(self, source: FeedSource, state: FetchState):
        request = urllib.request.Request(source.url, method="GET")
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept", "application/rss+xml, application/atom+xml, application/xml")
        if state.etag:
            request.add_header("If-None-Match", state.etag)
        if state.last_modified:
            request.add_header("If-Modified-Since", state.last_modified)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES)
                return (
                    body,
                    response.headers.get("ETag"),
                    response.headers.get("Last-Modified"),
                    False,
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return b"", state.etag, state.last_modified, True
            raise

    def state_for(self, name: str) -> FetchState:
        return self._state[name]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(body: bytes, source_name: str) -> tuple[Headline, ...]:
    """Parse RSS 2.0 or Atom into headlines. Tolerant of missing fields."""
    if not body.strip():
        return ()
    # XML from the open internet is untrusted; ElementTree does not expand
    # external entities, which is the property we need here.
    root = ET.fromstring(body)
    headlines: list[Headline] = []

    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = _text(item, "title")
        if not title:
            continue
        link = _text(item, "link") or _atom_link(item)
        published = (
            _parse_date(_text(item, "pubDate"))
            or _parse_date(_text(item, "published"))
            or _parse_date(_text(item, "updated"))
            or datetime.now(timezone.utc)
        )
        summary = _text(item, "description") or _text(item, "summary") or ""
        headlines.append(
            Headline(
                source=source_name,
                title=_clean(title)[:MAX_TITLE_CHARS],
                url=(link or "").strip()[:1000],
                published_at=published,
                summary=_clean(summary)[:MAX_TITLE_CHARS],
            )
        )
    return tuple(headlines)


def _text(element: ET.Element, name: str) -> str:
    for child in element:
        if child.tag.split("}")[-1] == name and child.text:
            return child.text
    return ""


def _atom_link(element: ET.Element) -> str:
    for child in element:
        if child.tag.split("}")[-1] == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return ""


_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """Strip markup and collapse whitespace. Headlines are data, not HTML."""
    import html

    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _parse_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _recent(
    headlines: Iterable[Headline], now: datetime, max_age: timedelta
) -> list[Headline]:
    cutoff = now - max_age
    return [h for h in headlines if h.published_at >= cutoff]


def _dedupe(headlines: Iterable[Headline]) -> list[Headline]:
    """Keep the first sighting of each story, newest first overall."""
    seen: set[str] = set()
    out: list[Headline] = []
    for headline in sorted(headlines, key=lambda h: h.published_at, reverse=True):
        key = headline.dedupe_key
        if key in seen:
            continue
        seen.add(key)
        out.append(headline)
    return out
