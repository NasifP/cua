"""The demo-mode guard. Every ambiguous case must resolve to 'do not click'."""

import time

import pytest

from egx_advisor.safety.demo_guard import (
    ActionRisk,
    DemoGuard,
    DemoState,
    DemoVerdict,
)
from egx_advisor.safety.pngutil import encode_png
from egx_advisor.safety.vision import ColourProbe, ColourSignature, Region

BLANK = encode_png(8, 8, bytes(8 * 8 * 3))


@pytest.fixture
def guard() -> DemoGuard:
    """A guard with no probes; evidence comes only from the injected tree."""
    return DemoGuard(text_probes=(), colour_probe=None)


def tree(*labels: str) -> dict:
    return {"role": "window", "children": [{"label": label} for label in labels]}


# --------------------------------------------------------------- fail-closed


def test_no_probe_available_is_indeterminate(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK)
    assert verdict.state is DemoState.INDETERMINATE
    assert not verdict.permits(ActionRisk.ORDER_CRITICAL)[0]


def test_empty_screenshot_is_indeterminate(guard: DemoGuard) -> None:
    assert guard.assert_demo(b"").state is DemoState.INDETERMINATE


def test_no_marker_found_is_indeterminate(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Portfolio", "Watchlist"))
    assert verdict.state is DemoState.INDETERMINATE


def test_read_only_is_permitted_without_an_assertion(guard: DemoGuard) -> None:
    """Reading the screen must work before anything can be asserted about it."""
    verdict = guard.assert_demo(BLANK)
    assert verdict.permits(ActionRisk.READ_ONLY)[0]


# ------------------------------------------------------------------ positive


@pytest.mark.parametrize(
    "label",
    ["Simulator", "Demo Account", "Virtual Portfolio", "Paper Trading",
     "محفظة تجريبية"],
)
def test_simulator_markers_confirm_demo(guard: DemoGuard, label: str) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree(label))
    assert verdict.state is DemoState.CONFIRMED_DEMO


def test_arabic_marker_matches_despite_diacritics(guard: DemoGuard) -> None:
    # Same phrase carrying diacritics and a ta-marbuta variant.
    verdict = guard.assert_demo(
        BLANK, accessibility_tree=tree("مَحفَظة تَجريبِية")
    )
    assert verdict.state is DemoState.CONFIRMED_DEMO


# ------------------------------------------------------------------ negative


@pytest.mark.parametrize(
    "label",
    ["Real Money", "Real Account", "Live Trading",
     "حساب حقيقي"],
)
def test_real_money_markers_confirm_live(guard: DemoGuard, label: str) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree(label))
    assert verdict.state is DemoState.CONFIRMED_LIVE
    assert not verdict.permits(ActionRisk.NAVIGATION)[0]


def test_live_evidence_beats_demo_evidence(guard: DemoGuard) -> None:
    """A screen showing both is a screen we do not understand. Treat it as live."""
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator", "Real Account"))
    assert verdict.state is DemoState.CONFIRMED_LIVE


def test_live_verdict_blocks_even_read_only_navigation(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Real Money"))
    assert not verdict.permits(ActionRisk.NAVIGATION)[0]
    assert not verdict.permits(ActionRisk.ORDER_CRITICAL)[0]


# ------------------------------------------------- false-positive trap guards


@pytest.mark.parametrize(
    "label",
    ["Real Estate", "TMGH Real Estate Sector", "live chart", "Live prices",
     "Realty Holdings"],
)
def test_ordinary_market_chrome_is_not_a_live_marker(guard: DemoGuard, label: str) -> None:
    """A bare 'real' or 'live' must not halt the bot.

    An EGX screen is full of "Real Estate" -- that is TMGH's own sector -- and of
    "live price" chrome. Treating those as real-money evidence would halt the bot
    permanently on a perfectly good simulator screen.
    """
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator", label))
    assert verdict.state is DemoState.CONFIRMED_DEMO


def test_demo_substring_does_not_match_inside_a_word(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("demography report"))
    assert verdict.state is DemoState.INDETERMINATE


# ----------------------------------------------------------------- freshness


def test_order_critical_requires_two_sources(guard: DemoGuard) -> None:
    """A weak single-source confirmation navigates but does not place orders."""
    verdict = DemoVerdict(
        state=DemoState.CONFIRMED_DEMO,
        confidence=0.6,
        evidence=(),
        asserted_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        monotonic_at=time.monotonic(),
        effective_sources=1,
    )
    assert verdict.permits(ActionRisk.NAVIGATION)[0]
    assert not verdict.permits(ActionRisk.ORDER_CRITICAL)[0]


def test_strong_published_label_counts_as_corroboration(guard: DemoGuard) -> None:
    """An accessibility label is the app's own statement, not an inference."""
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator"))
    assert verdict.effective_sources >= 2
    assert verdict.permits(ActionRisk.ORDER_CRITICAL)[0]


def test_stale_assertion_expires_for_order_critical(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator"))
    later = time.monotonic() + 3.0
    assert not verdict.permits(ActionRisk.ORDER_CRITICAL, now=later)[0]
    assert "old" in verdict.permits(ActionRisk.ORDER_CRITICAL, now=later)[1]


def test_very_stale_assertion_expires_for_navigation_too(guard: DemoGuard) -> None:
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator"))
    assert not verdict.permits(ActionRisk.NAVIGATION, now=time.monotonic() + 10)[0]


# -------------------------------------------------------------- probe errors


def test_a_raising_probe_does_not_crash_the_guard() -> None:
    class ExplodingProbe:
        name = "exploding"

        def read(self, screenshot: bytes, region: Region):
            raise RuntimeError("probe exploded")

    guard = DemoGuard(text_probes=(ExplodingProbe(),), colour_probe=None)
    verdict = guard.assert_demo(BLANK, accessibility_tree=tree("Simulator"))
    # The working probe still confirms; the broken one is simply unavailable.
    assert verdict.state is DemoState.CONFIRMED_DEMO
    assert "exploding" in verdict.probes_unavailable


def test_colour_probe_alone_cannot_authorise_an_order() -> None:
    """Colour is circumstantial. It corroborates; it never confirms on its own."""
    amber = (245, 166, 35)
    pixels = bytes(amber * (16 * 16))
    screenshot = encode_png(16, 16, pixels)
    guard = DemoGuard(
        text_probes=(),
        colour_probe=ColourProbe((ColourSignature("amber", amber),)),
    )
    verdict = guard.assert_demo(screenshot)
    assert verdict.state is DemoState.CONFIRMED_DEMO
    assert not verdict.permits(ActionRisk.ORDER_CRITICAL)[0]
    assert verdict.permits(ActionRisk.NAVIGATION)[0]


def test_undecodable_screenshot_leaves_colour_probe_unavailable() -> None:
    guard = DemoGuard(
        text_probes=(), colour_probe=ColourProbe((ColourSignature("amber", (1, 2, 3)),))
    )
    verdict = guard.assert_demo(b"not a png at all")
    assert verdict.state is DemoState.INDETERMINATE
