"""The USD/EGP journal: an absent opinion must not read as zero depreciation."""

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from egx_advisor.marketdata.fx import FxError, FxJournal, FxObservation

TODAY = date(2026, 9, 23)


@pytest.fixture
def journal(tmp_path) -> FxJournal:
    return FxJournal(tmp_path / "usd_egp.csv")


def test_records_and_reads_back(journal: FxJournal) -> None:
    journal.record(TODAY, D("51.57"))
    latest = journal.latest()
    assert latest is not None
    assert latest.usd_egp == D("51.57")
    assert latest.source == "screen"


def test_survives_a_reload(journal: FxJournal) -> None:
    journal.record(TODAY, D("51.57"))
    reopened = FxJournal(journal.path)
    assert reopened.latest().usd_egp == D("51.57")


def test_an_empty_journal_has_no_opinion(journal: FxJournal) -> None:
    """None, not zero. 'No opinion' and 'no depreciation' are different claims."""
    assert journal.depreciation(today=TODAY, lookback_days=180) is None
    assert not journal.spans(today=TODAY, lookback_days=180)


def test_a_single_observation_still_has_no_opinion(journal: FxJournal) -> None:
    journal.record(TODAY, D("51.57"))
    assert journal.depreciation(today=TODAY, lookback_days=180) is None


def test_depreciation_needs_the_window_covered(journal: FxJournal) -> None:
    journal.record(TODAY - timedelta(days=30), D("48.00"))
    journal.record(TODAY, D("51.57"))
    # 30 days of history cannot answer a 180-day question.
    assert journal.depreciation(today=TODAY, lookback_days=180) is None
    # It can answer a 30-day one.
    move = journal.depreciation(today=TODAY, lookback_days=30)
    assert move is not None and move > D("0.07")


def test_depreciation_over_a_covered_window(journal: FxJournal) -> None:
    journal.seed([
        FxObservation(TODAY - timedelta(days=200), D("38.00"), "seed"),
        FxObservation(TODAY - timedelta(days=100), D("48.00"), "seed"),
    ])
    journal.record(TODAY, D("51.57"))
    move = journal.depreciation(today=TODAY, lookback_days=180)
    assert move is not None
    assert D("0.35") < move < D("0.36")  # 38.00 -> 51.57
    assert journal.spans(today=TODAY, lookback_days=180)


def test_an_implausible_rate_is_rejected_not_stored(journal: FxJournal) -> None:
    """A misread is not a currency event."""
    with pytest.raises(FxError, match="plausible band"):
        journal.record(TODAY, D("515.70"))
    assert journal.latest() is None


def test_an_implausible_jump_is_rejected(journal: FxJournal) -> None:
    """A value inside the band can still be an impossible move."""
    journal.record(TODAY - timedelta(days=1), D("51.57"))
    with pytest.raises(FxError, match="refusing to record"):
        journal.record(TODAY, D("180.00"))
    # The good observation survives; the bad one never lands.
    assert journal.latest().usd_egp == D("51.57")


def test_a_lost_decimal_point_is_rejected(journal: FxJournal) -> None:
    """The likeliest OCR failure on 51.57 is 5.157, an order of magnitude out.

    A band anchored near zero waves this through, which is why the floor sits
    well above any rate this system will legitimately observe live.
    """
    with pytest.raises(FxError, match="plausible band"):
        journal.record(TODAY, D("5.157"))
    assert journal.latest() is None


def test_a_real_devaluation_is_still_recordable(journal: FxJournal) -> None:
    """The jump guard must not block an actual repricing."""
    journal.record(TODAY - timedelta(days=1), D("48.00"))
    journal.record(TODAY, D("62.00"))  # ~29%, large but real
    assert journal.latest().usd_egp == D("62.00")


def test_seed_does_not_overwrite_observations(journal: FxJournal) -> None:
    journal.record(TODAY, D("51.57"), source="screen")
    added = journal.seed([FxObservation(TODAY, D("49.00"), "provider")])
    assert added == 0
    assert journal.latest().usd_egp == D("51.57")
    assert journal.latest().source == "screen"


def test_rate_on_or_before_walks_back_to_the_last_session(journal: FxJournal) -> None:
    journal.record(TODAY - timedelta(days=3), D("51.00"))
    found = journal.rate_on_or_before(TODAY)
    assert found is not None and found.usd_egp == D("51.00")


def test_a_corrupt_row_raises_rather_than_being_skipped(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("date,usd_egp,source\n2026-09-23,not-a-number,screen\n", encoding="utf-8")
    with pytest.raises(FxError):
        FxJournal(path).load()
