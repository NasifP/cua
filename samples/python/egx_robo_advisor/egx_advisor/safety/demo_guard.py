"""Demo-mode assertion: the gate between the bot and a real-money account.

Contract
--------
`DemoGuard.assert_demo()` answers one question -- "is the screen I am about to
click on the Thndr simulator?" -- and it answers it in one of three ways:

    CONFIRMED_DEMO   positive evidence, no contradicting evidence  -> may click
    CONFIRMED_LIVE   real-money evidence found                     -> halt, hard
    INDETERMINATE    not enough evidence either way                -> halt

There is no fourth answer and no "probably". INDETERMINATE and CONFIRMED_LIVE
both stop the bot, so every failure mode of every probe -- missing dependency,
OCR garbage, unfamiliar layout, decode error, exception -- lands on "do not
click". That is the only defensible default when the downside is an unintended
order in someone's actual portfolio.

Why several probes rather than one
----------------------------------
A single OCR read of a single region is a single point of failure, and the cost
of a false "yes" is unbounded. The guard therefore collects evidence from
independent families (published accessibility labels, glyphs, accent colour) in
two regions, and requires corroboration proportional to the risk of the action.

Freshness
---------
An assertion is a statement about a moment, not a standing permit. Account mode
can change between the check and the click -- the user taps the account switcher
on their phone, a deep link opens the live tab, the app restores a session. So
every verdict carries `asserted_at`, callers declare how stale they tolerate,
and order-critical actions re-assert immediately before *and* after acting.

False-positive traps this deliberately avoids
---------------------------------------------
Bare "real" and "live" are NOT real-money markers. An EGX screen is full of
"Real Estate" (that is TMGH's sector) and "live price"/"live chart" chrome.
Treating those as real-money evidence would halt the bot permanently on a
perfectly good simulator screen, and a guard that cries wolf gets switched off.
Negative markers are therefore specific multi-word phrases only.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from .vision import (
    HEADER_REGION,
    TICKET_REGION,
    AccessibilityTextProbe,
    ColourProbe,
    ColourSignature,
    ProbeResult,
    Region,
    TesseractTextProbe,
    TextProbe,
    contains_token,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Markers
# --------------------------------------------------------------------------- #

#: Evidence that the simulator is active. English and Arabic, because the app's
#: language follows the phone's.
DEMO_TOKENS: tuple[str, ...] = (
    "simulator",
    "simulated",
    "virtual",
    "virtual portfolio",
    "demo",
    "demo account",
    "paper trading",
    "practice account",
    "محاكي",  # muhaki (simulator)
    "تجريبي",  # tajribi (trial)
    "تجريبية",  # tajribiya
    "محفظة تجريبية",  # trial portfolio
    "افتراضي",  # iftiradi (virtual)
    "افتراضية",
)

#: Evidence that real money is on screen. Every entry is specific enough that it
#: cannot collide with ordinary market chrome -- see the module docstring.
LIVE_TOKENS: tuple[str, ...] = (
    "real money",
    "real account",
    "real portfolio",
    "live account",
    "live portfolio",
    "live trading",
    "real funds",
    "حساب حقيقي",  # real account
    "محفظة حقيقية",  # real portfolio
    "أموال حقيقية",  # real funds
    "تداول حقيقي",  # real trading
)

#: Accent colours Thndr's simulator chip has used. Circumstantial corroboration
#: only; never sufficient alone. Re-measure from a screenshot when the app
#: restyles, and treat a mismatch as "no signal", not as live-money evidence.
DEMO_COLOUR_SIGNATURES: tuple[ColourSignature, ...] = (
    ColourSignature("simulator-amber", (245, 166, 35), tolerance=45),
    ColourSignature("simulator-violet", (124, 77, 255), tolerance=45),
)


class DemoState(enum.Enum):
    CONFIRMED_DEMO = "confirmed_demo"
    CONFIRMED_LIVE = "confirmed_live"
    INDETERMINATE = "indeterminate"

    @property
    def may_click(self) -> bool:
        return self is DemoState.CONFIRMED_DEMO


class ActionRisk(enum.Enum):
    """How much corroboration an action has to earn before it reaches the screen."""

    #: Screenshots, scrolling, reading. Cannot move money.
    READ_ONLY = "read_only"
    #: Taps that navigate. Could in principle land on something destructive.
    NAVIGATION = "navigation"
    #: Anything that can create, size, or confirm an order.
    ORDER_CRITICAL = "order_critical"

    @property
    def required_sources(self) -> int:
        return {"read_only": 0, "navigation": 1, "order_critical": 2}[self.value]

    @property
    def max_age_seconds(self) -> float:
        """How old an assertion may be when this action fires.

        This is a backstop, not the primary defence: GuardedInterface
        re-asserts before every mutating call by default, so a verdict is
        normally milliseconds old. The window only bounds the damage if someone
        turns `reassert_every_action` off to trade safety for throughput.
        """
        return {"read_only": 30.0, "navigation": 2.0, "order_critical": 1.5}[self.value]


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    region: str
    token: str
    polarity: str  # "demo" | "live"
    confidence: float
    excerpt: str = ""

    def describe(self) -> str:
        return f"{self.polarity}:{self.token!r} via {self.source}/{self.region} @{self.confidence:.2f}"


@dataclass(frozen=True, slots=True)
class DemoVerdict:
    state: DemoState
    confidence: float
    evidence: tuple[Evidence, ...]
    asserted_at: datetime
    #: Monotonic stamp, used for age checks. Wall-clock can jump; this cannot.
    monotonic_at: float
    probes_run: tuple[str, ...] = ()
    probes_unavailable: tuple[str, ...] = ()
    detail: str = ""
    #: Corroborating demo sources *after* the strong-label upgrade in
    #: DemoGuard._decide. `permits()` must use this rather than recounting
    #: distinct sources, or a strong published label gets under-credited.
    effective_sources: int = 0

    @property
    def demo_sources(self) -> frozenset[str]:
        return frozenset(e.source for e in self.evidence if e.polarity == "demo")

    @property
    def live_evidence(self) -> tuple[Evidence, ...]:
        return tuple(e for e in self.evidence if e.polarity == "live")

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.monotonic()) - self.monotonic_at

    def permits(self, risk: ActionRisk, *, now: Optional[float] = None) -> tuple[bool, str]:
        """Whether this verdict authorises `risk` right now, and why not if it doesn't."""
        if self.state is DemoState.CONFIRMED_LIVE:
            return False, f"real-money environment detected ({self._live_summary()})"
        if risk is ActionRisk.READ_ONLY:
            # Reading the screen cannot move money, and the portfolio read has to
            # happen before we can assert anything about it at all.
            return True, "read-only action"
        if self.state is not DemoState.CONFIRMED_DEMO:
            return False, f"demo mode not confirmed: {self.detail or 'no positive evidence'}"
        age = self.age_seconds(now)
        if age > risk.max_age_seconds:
            return False, (
                f"assertion is {age:.1f}s old, {risk.value} requires "
                f"< {risk.max_age_seconds:.1f}s"
            )
        sources = max(self.effective_sources, len(self.demo_sources))
        if sources < risk.required_sources:
            return False, (
                f"{sources} corroborating source(s), {risk.value} requires "
                f"{risk.required_sources}"
            )
        return True, f"demo confirmed by {sources} source(s), {age:.1f}s old"

    def _live_summary(self) -> str:
        return ", ".join(e.describe() for e in self.live_evidence) or "unspecified"

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "confidence": round(self.confidence, 3),
            "asserted_at": self.asserted_at.isoformat(),
            "age_seconds": round(self.age_seconds(), 2),
            "evidence": [e.describe() for e in self.evidence],
            "effective_sources": self.effective_sources,
            "probes_run": list(self.probes_run),
            "probes_unavailable": list(self.probes_unavailable),
            "detail": self.detail,
        }


class DemoModeViolation(RuntimeError):
    """Raised instead of clicking. Never caught inside the execution layer."""

    def __init__(self, message: str, verdict: Optional[DemoVerdict] = None) -> None:
        super().__init__(message)
        self.verdict = verdict


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #


@dataclass
class DemoGuard:
    """Collects evidence and renders a verdict. Holds no authority to click."""

    text_probes: Sequence[TextProbe] = field(default_factory=lambda: (TesseractTextProbe(),))
    colour_probe: Optional[ColourProbe] = None
    regions: Sequence[Region] = (HEADER_REGION, TICKET_REGION)
    demo_tokens: Sequence[str] = DEMO_TOKENS
    live_tokens: Sequence[str] = LIVE_TOKENS
    #: A single text hit this confident counts as corroboration on its own,
    #: because an explicit published label is strong evidence.
    strong_confidence: float = 0.9

    @classmethod
    def default(cls, *, colour: bool = True) -> "DemoGuard":
        return cls(
            text_probes=(TesseractTextProbe(),),
            colour_probe=ColourProbe(DEMO_COLOUR_SIGNATURES) if colour else None,
        )

    def assert_demo(
        self,
        screenshot: bytes,
        *,
        accessibility_tree: Optional[Mapping[str, Any]] = None,
    ) -> DemoVerdict:
        """Inspect one screenshot and return a verdict. Never raises."""
        now_wall = datetime.now(timezone.utc)
        now_mono = time.monotonic()

        probes: list[TextProbe] = list(self.text_probes)
        if accessibility_tree:
            # Prepended: the app's own labels outrank anything inferred from pixels.
            probes.insert(0, AccessibilityTextProbe(accessibility_tree))

        evidence: list[Evidence] = []
        ran: list[str] = []
        unavailable: list[str] = []

        if not screenshot:
            return DemoVerdict(
                DemoState.INDETERMINATE,
                0.0,
                (),
                now_wall,
                now_mono,
                detail="empty screenshot",
            )

        for probe in probes:
            probe_ran = False
            for region in self.regions:
                try:
                    result = probe.read(screenshot, region)
                except Exception as exc:  # noqa: BLE001 - a broken probe must not crash the loop
                    logger.warning("probe %s raised: %s", probe.name, exc)
                    result = ProbeResult(probe.name, False, detail=str(exc))
                if not result.available:
                    continue
                probe_ran = True
                evidence.extend(self._scan_text(result, region))
            (ran if probe_ran else unavailable).append(probe.name)

        if self.colour_probe is not None:
            ok, findings = self.colour_probe.read(screenshot, HEADER_REGION)
            if ok:
                ran.append(self.colour_probe.name)
                for finding in findings:
                    if finding.matched:
                        evidence.append(
                            Evidence(
                                source=self.colour_probe.name,
                                region=HEADER_REGION.name,
                                token=finding.signature.label,
                                polarity="demo",
                                confidence=min(0.75, 0.5 + finding.coverage * 10),
                                excerpt=f"coverage {finding.coverage:.4f}",
                            )
                        )
            else:
                unavailable.append(self.colour_probe.name)

        return self._decide(tuple(evidence), now_wall, now_mono, tuple(ran), tuple(unavailable))

    # ------------------------------------------------------------------ internals

    def _scan_text(self, result: ProbeResult, region: Region) -> list[Evidence]:
        """Match markers against a probe's output.

        Matched against the joined text, not word by word, so multi-word phrases
        like "real money" and "محفظة تجريبية" are detectable even when the
        probe hands back one word per hit.
        """
        joined = result.joined_text
        if not joined.strip():
            return []
        best = max((h.confidence for h in result.hits), default=0.0)
        found: list[Evidence] = []
        for token in self.live_tokens:
            if contains_token(joined, token):
                found.append(
                    Evidence(result.source, region.name, token, "live", max(best, 0.5),
                             _excerpt(joined, token))
                )
        for token in self.demo_tokens:
            if contains_token(joined, token):
                found.append(
                    Evidence(result.source, region.name, token, "demo", best,
                             _excerpt(joined, token))
                )
        return found

    def _decide(
        self,
        evidence: tuple[Evidence, ...],
        now_wall: datetime,
        now_mono: float,
        ran: tuple[str, ...],
        unavailable: tuple[str, ...],
    ) -> DemoVerdict:
        live = [e for e in evidence if e.polarity == "live"]
        demo = [e for e in evidence if e.polarity == "demo"]

        # Rule 1: any real-money evidence wins outright, even alongside demo
        # evidence. A screen showing both is a screen we do not understand, and
        # "ambiguous" must never resolve towards clicking.
        if live:
            return DemoVerdict(
                DemoState.CONFIRMED_LIVE,
                max(e.confidence for e in live),
                evidence,
                now_wall,
                now_mono,
                ran,
                unavailable,
                detail=(
                    "real-money marker(s) present"
                    + (" alongside demo markers, treated as live" if demo else "")
                ),
            )

        if not ran:
            return DemoVerdict(
                DemoState.INDETERMINATE, 0.0, evidence, now_wall, now_mono, ran, unavailable,
                detail="no vision probe could run; install pytesseract+Pillow or supply an "
                       "accessibility tree",
            )

        if not demo:
            return DemoVerdict(
                DemoState.INDETERMINATE, 0.0, evidence, now_wall, now_mono, ran, unavailable,
                detail=f"no simulator marker found by {', '.join(ran)}",
            )

        # Rule 2: a strongly-confident published label counts as two sources, so
        # an accessibility hit alone can authorise an order. Anything weaker needs
        # a genuinely independent second source.
        sources = {e.source for e in demo}
        effective = len(sources)
        strong = [e for e in demo if e.confidence >= self.strong_confidence]
        if strong and effective < 2:
            effective = 2

        confidence = min(0.99, max(e.confidence for e in demo) * (1 + 0.1 * (effective - 1)))
        return DemoVerdict(
            DemoState.CONFIRMED_DEMO,
            confidence,
            evidence,
            now_wall,
            now_mono,
            ran,
            unavailable,
            effective_sources=effective,
            detail=(
                f"simulator confirmed by {sorted(sources)}"
                + (" (strong label counts as corroborated)" if strong and len(sources) < 2 else "")
            ),
        )


def _excerpt(text: str, token: str, width: int = 48) -> str:
    from .vision import normalise

    hay = normalise(text)
    needle = normalise(token)
    index = hay.find(needle)
    if index < 0:
        return hay[:width]
    start = max(0, index - width // 3)
    return hay[start : start + width]
