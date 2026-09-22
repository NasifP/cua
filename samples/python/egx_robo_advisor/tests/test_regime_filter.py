"""The circuit breaker, and the invariant that it can only ever subtract."""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from egx_advisor.regime.filter import (
    FilterConfig,
    RegimeFilter,
    SubtractiveInvariantViolation,
    _assert_subtractive,
)
from egx_advisor.regime.sentiment import (
    POSITIVE_SENTIMENT_IS_IGNORED,
    KeywordClassifier,
    combine,
)
from egx_advisor.types import (
    Headline,
    NewsAssessment,
    ProposedOrder,
    RiskState,
    Scope,
    Severity,
    Side,
)

NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


def headline(title: str) -> Headline:
    return Headline("test", title, f"https://example.test/{hash(title)}", NOW)


def orders() -> tuple[ProposedOrder, ...]:
    return (
        ProposedOrder("COMI.CA", Side.BUY, Decimal(100), Decimal(85), "drift"),
        ProposedOrder("EAST.CA", Side.BUY, Decimal(200), Decimal(40), "drift"),
        ProposedOrder("TMGH.CA", Side.SELL, Decimal(300), Decimal(52), "drift"),
    )


@pytest.fixture
def classifier() -> KeywordClassifier:
    return KeywordClassifier()


# ----------------------------------------------------------- the invariant


def test_filter_output_is_always_a_subset() -> None:
    plan = orders()
    regime_filter = RegimeFilter()
    for state in (
        regime_filter.evaluate([], now=NOW, feed_age_seconds=60),
        regime_filter.panic("test"),
    ):
        allowed, suppressed = regime_filter.apply(plan, state)
        assert set(allowed).issubset(set(plan))
        assert len(allowed) + len(suppressed) == len(plan)


def test_subtractive_invariant_rejects_extra_orders() -> None:
    original = orders()
    tampered = original + (
        ProposedOrder("SWDY.CA", Side.BUY, Decimal(50), Decimal(70), "invented"),
    )
    with pytest.raises(SubtractiveInvariantViolation, match="returned 4 orders from 3"):
        _assert_subtractive(original, tampered)


def test_subtractive_invariant_rejects_a_substituted_symbol() -> None:
    """Same order count, but one name swapped for another the plan never held."""
    original = orders()
    substituted = original[:2] + (
        ProposedOrder("SWDY.CA", Side.BUY, Decimal(50), Decimal(70), "invented"),
    )
    with pytest.raises(SubtractiveInvariantViolation, match="never contained"):
        _assert_subtractive(original, substituted)


def test_subtractive_invariant_rejects_an_enlarged_order() -> None:
    original = orders()
    enlarged = (original[0].scaled_to(Decimal(100), "x"),)
    bigger = (ProposedOrder("COMI.CA", Side.BUY, Decimal(500), Decimal(85), "pumped"),)
    _assert_subtractive(original, enlarged)  # shrinking is fine
    with pytest.raises(SubtractiveInvariantViolation, match="enlarged"):
        _assert_subtractive(original, bigger)


def test_subtractive_invariant_rejects_a_side_flip() -> None:
    original = (ProposedOrder("COMI.CA", Side.SELL, Decimal(100), Decimal(85), "drift"),)
    flipped = (ProposedOrder("COMI.CA", Side.BUY, Decimal(100), Decimal(85), "flipped"),)
    with pytest.raises(SubtractiveInvariantViolation):
        _assert_subtractive(original, flipped)


def test_invariant_holds_over_randomised_plans_and_regimes() -> None:
    """Property check: whatever the news, the filter never adds or enlarges."""
    rng = random.Random(20260922)
    symbols = ["COMI.CA", "TMGH.CA", "SWDY.CA", "ABUK.CA", "AZG.CA", "EAST.CA"]
    titles = [
        "Central bank devalues the Egyptian pound",
        "EGX halts trading in SWDY.CA",
        "Record profits announced, shares surge 20%",
        "Analysts see huge upside for COMI.CA",
        "Auditor resigns citing accounting irregularities",
        "Nothing much happened today",
    ]
    classifier = KeywordClassifier()

    for _ in range(300):
        plan = tuple(
            ProposedOrder(
                rng.choice(symbols),
                rng.choice([Side.BUY, Side.SELL]),
                Decimal(rng.randint(1, 5000)),
                Decimal(rng.randint(1, 200)),
                "drift",
            )
            for _ in range(rng.randint(0, 6))
        )
        regime_filter = RegimeFilter()
        assessments = classifier.classify(
            [headline(rng.choice(titles)) for _ in range(rng.randint(0, 4))]
        )
        regime = regime_filter.evaluate(
            assessments,
            now=NOW,
            feed_age_seconds=rng.choice([30, 60, 100_000, None]),
            in_session=rng.choice([True, False]),
        )
        allowed, suppressed = regime_filter.apply(plan, regime)

        assert len(allowed) <= len(plan)
        assert len(allowed) + len(suppressed) == len(plan)
        for order in allowed:
            assert order in plan, "filter produced an order the plan never had"


def test_good_news_never_creates_or_enlarges_an_order() -> None:
    """The headline finding that motivated the whole design."""
    regime_filter = RegimeFilter()
    classifier = KeywordClassifier()
    bullish = classifier.classify(
        [
            headline("COMI.CA smashes earnings, shares surge 20%"),
            headline("EGX30 hits an all-time high on record foreign inflows"),
        ]
    )
    regime = regime_filter.evaluate(bullish, now=NOW, feed_age_seconds=60)
    plan = orders()
    allowed, _ = regime_filter.apply(plan, regime)
    assert allowed == plan, "good news must leave the plan exactly as it was"
    assert all(a.severity is Severity.NONE for a in bullish)


def test_positive_sentiment_flag_is_asserted() -> None:
    assert POSITIVE_SENTIMENT_IS_IGNORED is True


# --------------------------------------------------------------- behaviour


def test_clean_feeds_permit_everything() -> None:
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate([], now=NOW, feed_age_seconds=60)
    assert regime.risk_state is RiskState.RISK_ON
    allowed, suppressed = regime_filter.apply(orders(), regime)
    assert len(allowed) == 3 and not suppressed


def test_market_catastrophe_halts_buys_but_not_sells(classifier: KeywordClassifier) -> None:
    """De-risking during a crisis is the behaviour we want to preserve."""
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate(
        classifier.classify([headline("Central bank devalues the Egyptian pound by 20%")]),
        now=NOW,
        feed_age_seconds=60,
    )
    assert regime.risk_state is RiskState.BUYS_HALTED
    allowed, suppressed = regime_filter.apply(orders(), regime)
    assert [o.side for o in allowed] == [Side.SELL]
    assert len(suppressed) == 2


def test_single_name_catastrophe_blocks_only_that_symbol(
    classifier: KeywordClassifier,
) -> None:
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate(
        classifier.classify([headline("EGX halts trading in EAST.CA pending disclosure")]),
        now=NOW,
        feed_age_seconds=60,
    )
    assert regime.risk_state is RiskState.RISK_ON
    assert "EAST.CA" in regime.blocked_symbols
    allowed, suppressed = regime_filter.apply(orders(), regime)
    assert {o.symbol for o in allowed} == {"COMI.CA", "TMGH.CA"}
    assert suppressed[0][0].symbol == "EAST.CA"


def test_stale_feeds_halt_buying() -> None:
    """Absence of bad news is not evidence of calm; it usually means a broken poller."""
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate([], now=NOW, feed_age_seconds=7200, in_session=True)
    assert regime.risk_state is RiskState.BUYS_HALTED
    assert regime.stale


def test_never_polled_feeds_halt_buying() -> None:
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate([], now=NOW, feed_age_seconds=None, in_session=True)
    assert regime.risk_state is RiskState.BUYS_HALTED


def test_quiet_feeds_out_of_session_are_not_treated_as_stale() -> None:
    """Outside trading hours a silent feed is normal and must not latch a halt."""
    regime_filter = RegimeFilter()
    regime = regime_filter.evaluate([], now=NOW, feed_age_seconds=7200, in_session=False)
    assert regime.risk_state is RiskState.RISK_ON


def test_panic_halts_everything_including_sells() -> None:
    regime_filter = RegimeFilter()
    regime = regime_filter.panic("portfolio read disagreed with the screen")
    assert regime.risk_state is RiskState.ALL_HALTED
    allowed, suppressed = regime_filter.apply(orders(), regime)
    assert not allowed and len(suppressed) == 3


def test_pile_up_of_elevated_items_halts_buying() -> None:
    config = FilterConfig(elevated_items_for_halt=3)
    regime_filter = RegimeFilter(config=config)
    elevated = tuple(
        NewsAssessment(headline(f"profit warning {i}"), Severity.ELEVATED, Scope.MARKET)
        for i in range(3)
    )
    regime = regime_filter.evaluate(elevated, now=NOW, feed_age_seconds=60)
    assert regime.risk_state is RiskState.BUYS_HALTED


# -------------------------------------------------------------- hysteresis


def test_recovery_requires_cooldown_and_clean_polls(classifier: KeywordClassifier) -> None:
    config = FilterConfig(market_cooldown=timedelta(hours=1), clean_polls_to_recover=3)
    regime_filter = RegimeFilter(config=config)

    regime_filter.evaluate(
        classifier.classify([headline("Central bank devalues the Egyptian pound")]),
        now=NOW,
        feed_age_seconds=60,
    )

    # Still inside the cooldown.
    mid = regime_filter.evaluate([], now=NOW + timedelta(minutes=30), feed_age_seconds=60)
    assert mid.risk_state is RiskState.BUYS_HALTED

    # Cooldown elapsed, but not enough clean polls yet.
    after = NOW + timedelta(hours=2)
    first = regime_filter.evaluate([], now=after, feed_age_seconds=60)
    assert first.risk_state is RiskState.BUYS_HALTED

    regime_filter.evaluate([], now=after, feed_age_seconds=60)
    third = regime_filter.evaluate([], now=after, feed_age_seconds=60)
    assert third.risk_state is RiskState.RISK_ON


def test_symbol_block_expires_after_its_cooldown(classifier: KeywordClassifier) -> None:
    config = FilterConfig(symbol_cooldown=timedelta(hours=2))
    regime_filter = RegimeFilter(config=config)
    regime_filter.evaluate(
        classifier.classify([headline("EGX halts trading in EAST.CA")]),
        now=NOW,
        feed_age_seconds=60,
    )
    later = regime_filter.evaluate([], now=NOW + timedelta(hours=3), feed_age_seconds=60)
    assert "EAST.CA" not in later.blocked_symbols


# ------------------------------------------------------------- composition


def test_combine_keeps_the_harshest_verdict() -> None:
    """The LLM may raise severity; it must never lower it."""
    item = headline("Something ambiguous happened at the group")
    mild = (NewsAssessment(item, Severity.NONE, Scope.MARKET, reason="keyword saw nothing"),)
    harsh = (NewsAssessment(item, Severity.CATASTROPHIC, Scope.MARKET, reason="llm saw fraud"),)

    assert combine(mild, harsh)[0].severity is Severity.CATASTROPHIC
    assert combine(harsh, mild)[0].severity is Severity.CATASTROPHIC, "order must not matter"


def test_combine_unions_identified_symbols() -> None:
    item = headline("Trouble at two issuers")
    left = (NewsAssessment(item, Severity.ELEVATED, Scope.TICKER, symbols=("COMI.CA",)),)
    right = (NewsAssessment(item, Severity.ELEVATED, Scope.TICKER, symbols=("TMGH.CA",)),)
    merged = combine(left, right)[0]
    assert set(merged.symbols) == {"COMI.CA", "TMGH.CA"}


def test_an_absent_llm_leaves_the_keyword_verdict_standing() -> None:
    item = headline("Central bank devalues the Egyptian pound")
    keyword = KeywordClassifier().classify([item])
    assert combine(keyword, ())[0].severity is Severity.CATASTROPHIC


def test_unattributed_single_name_catastrophe_escalates_to_market_scope() -> None:
    """We cannot block a ticker we cannot name, so we stand down more broadly."""
    assessment = KeywordClassifier().classify_one(
        headline("Auditor resigns citing accounting irregularities")
    )
    assert assessment.severity is Severity.CATASTROPHIC
    assert assessment.scope is Scope.MARKET


# ------------------------------------------------------------- panic latching


def test_panic_is_latched_against_a_subsequent_clean_poll() -> None:
    """A panic means we distrust our own picture; a calm feed is not a reason to
    resume. Without the latch, the next poll silently downgrades ALL_HALTED to
    BUYS_HALTED and the bot is back on the screen."""
    regime_filter = RegimeFilter()
    regime_filter.panic("portfolio read disagreed with the screen", now=NOW)

    later = regime_filter.evaluate([], now=NOW + timedelta(minutes=5), feed_age_seconds=60)

    assert later.risk_state is RiskState.ALL_HALTED
    assert "latched" in later.drivers[0]


def test_panic_latch_blocks_sells_too() -> None:
    regime_filter = RegimeFilter()
    regime_filter.panic("data integrity failure", now=NOW)
    regime = regime_filter.evaluate([], now=NOW + timedelta(minutes=5), feed_age_seconds=60)
    allowed, suppressed = regime_filter.apply(orders(), regime)
    assert not allowed and len(suppressed) == 3


def test_panic_latch_expires_on_its_timer() -> None:
    regime_filter = RegimeFilter(config=FilterConfig(market_cooldown=timedelta(hours=1)))
    regime_filter.panic("transient failure", now=NOW)
    after = NOW + timedelta(hours=2)
    # The latch has expired, though the clean-poll hysteresis still applies.
    state = regime_filter.evaluate([], now=after, feed_age_seconds=60)
    assert state.risk_state is not RiskState.ALL_HALTED


def test_panic_can_be_cleared_explicitly() -> None:
    regime_filter = RegimeFilter()
    regime_filter.panic("operator investigating", now=NOW)
    assert regime_filter.panicking
    regime_filter.clear_panic(actor="operator")
    assert not regime_filter.panicking
    state = regime_filter.evaluate([], now=NOW, feed_age_seconds=60)
    assert state.risk_state is not RiskState.ALL_HALTED
