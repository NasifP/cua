"""Market-data providers, and the rule that bad data blocks rather than degrades."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from egx_advisor.marketdata import (
    DataQualityError,
    JsonFileMarketData,
    MarketDataError,
    QualityPolicy,
    Severity,
    YahooMarketData,
    YahooRow,
    validate_bars,
    validate_quotes,
)
from egx_advisor.marketdata.yahoo import USD_EGP_SYMBOL
from egx_advisor.strategy.policy import AllocationPolicy
from egx_advisor.types import Instrument, Quote, Sleeve, Tradability

POLICY = AllocationPolicy()
UNIVERSE = POLICY.universe
TODAY = date.today()


def rows(symbol_close: str = "85", days: int = 5, **overrides) -> list[YahooRow]:
    """A clean ascending series ending today."""
    out = []
    base = D(symbol_close)
    for i in range(days):
        day = TODAY - timedelta(days=days - 1 - i)
        close = base + D(i) / D(10)
        out.append(
            YahooRow(
                day=day,
                open=close,
                high=close + D("0.5"),
                low=close - D("0.5"),
                close=close,
                volume=D("100000"),
                currency=overrides.get("currency", "EGP"),
            )
        )
    return out


def fetch_for(overrides: dict | None = None, *, include_fx: bool = True):
    """Build an injected fetch returning clean data, with per-symbol overrides."""
    overrides = overrides or {}

    def fetch(symbols, lookback_days):
        out = {}
        for symbol in symbols:
            if symbol == USD_EGP_SYMBOL:
                if include_fx:
                    out[symbol] = rows("48", days=200)
                continue
            if symbol in overrides:
                value = overrides[symbol]
                if value is not None:
                    out[symbol] = value
                continue
            out[symbol] = rows()
        return out

    return fetch


# --------------------------------------------------------------- happy path


async def test_snapshot_maps_rows_to_quotes() -> None:
    provider = YahooMarketData(fetch=fetch_for())
    snapshot = await provider.snapshot(UNIVERSE)
    assert set(snapshot.quotes) == {i.symbol for i in UNIVERSE}
    quote = snapshot.quote("COMI.CA")
    assert quote.last == D("85.4")      # last row
    assert quote.prev_close == D("85.3")  # the row before it
    assert quote.tradability is Tradability.OPEN


async def test_macro_series_drives_the_depreciation_reading() -> None:
    def fetch(symbols, lookback_days):
        out = {i.symbol: rows() for i in UNIVERSE}
        fx = []
        for i in range(200):
            day = TODAY - timedelta(days=199 - i)
            # A step devaluation two-thirds of the way through.
            level = D("48") if i < 130 else D("62")
            fx.append(YahooRow(day, level, level, level, level, D(0), "EGP"))
        out[USD_EGP_SYMBOL] = fx
        return out

    provider = YahooMarketData(fetch=fetch, macro_lookback_days=180)
    snapshot = await provider.snapshot(UNIVERSE)
    assert snapshot.usd_egp == D("62")
    assert snapshot.egp_depreciation > D("0.25")
    assert POLICY.in_devaluation_state(snapshot.egp_depreciation)


async def test_missing_fx_leaves_the_policy_at_baseline() -> None:
    """A missing FX series must fail to DETECT a devaluation, never trigger one."""
    provider = YahooMarketData(fetch=fetch_for(include_fx=False))
    snapshot = await provider.snapshot(UNIVERSE)
    assert snapshot.egp_depreciation == 0
    assert not POLICY.in_devaluation_state(snapshot.egp_depreciation)


# ----------------------------------------------------------- blocking faults


async def test_missing_symbol_blocks_the_snapshot() -> None:
    """A universe member with no data is an unknown weight, not a zero one."""
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": None}))
    with pytest.raises(DataQualityError) as exc:
        await provider.snapshot(UNIVERSE)
    assert any(f.code == "MISSING_SYMBOL" for f in exc.value.report.blocking)


async def test_wrong_currency_blocks_the_snapshot() -> None:
    """A USD figure treated as EGP would scale every order by roughly 50x."""
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": rows(currency="USD")}))
    with pytest.raises(DataQualityError) as exc:
        await provider.snapshot(UNIVERSE)
    assert any(f.code == "CURRENCY_MISMATCH" for f in exc.value.report.blocking)


async def test_unadjusted_split_blocks_the_snapshot() -> None:
    """An overnight halving is a corporate action, not a price to size against."""
    split = rows()
    split[-1] = YahooRow(
        split[-1].day, D("42"), D("43"), D("41"), D("42"), D("100000"), "EGP"
    )
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": split}))
    with pytest.raises(DataQualityError) as exc:
        await provider.snapshot(UNIVERSE)
    assert any(f.code == "IMPLAUSIBLE_MOVE" for f in exc.value.report.blocking)


async def test_non_positive_price_blocks_the_snapshot() -> None:
    bad = rows()
    bad[-1] = YahooRow(bad[-1].day, D(1), D(1), D(0), D(0), D(1), "EGP")
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": bad}))
    with pytest.raises(DataQualityError):
        await provider.snapshot(UNIVERSE)


async def test_fetch_failure_raises_rather_than_returning_partial_data() -> None:
    def boom(symbols, lookback_days):
        raise ConnectionError("yahoo unreachable")

    with pytest.raises(MarketDataError, match="yahoo fetch failed"):
        await YahooMarketData(fetch=boom).snapshot(UNIVERSE)


async def test_empty_response_raises() -> None:
    with pytest.raises(MarketDataError, match="no data"):
        await YahooMarketData(fetch=lambda s, d: {}).snapshot(UNIVERSE)


async def test_enforce_quality_false_lets_data_through_but_records_it() -> None:
    """The escape hatch exists for exploration and must still report."""
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": None}), enforce_quality=False)
    snapshot = await provider.snapshot(UNIVERSE)
    assert "COMI.CA" not in snapshot.quotes
    assert provider.last_report.blocking


# --------------------------------------------------------------- halts


async def test_halts_are_never_inferred_from_price_data() -> None:
    """Yahoo cannot tell a suspension from a holiday; claiming otherwise misleads."""
    quiet = rows()
    quiet[-1] = YahooRow(
        quiet[-1].day, D("85"), D("85"), D("85"), D("85"), D(0), "EGP"
    )
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": quiet}))
    snapshot = await provider.snapshot(UNIVERSE)
    assert snapshot.quote("COMI.CA").tradability is not Tradability.HALTED
    # Zero volume is surfaced as a warning, not silently ignored.
    assert any(f.code == "ZERO_VOLUME" for f in provider.last_report.warnings)


# ------------------------------------------------------------ validators


def test_validate_quotes_flags_a_missing_member() -> None:
    report = validate_quotes({}, UNIVERSE)
    assert not report.ok
    assert len(report.blocking) == len(UNIVERSE)


def test_validate_quotes_accepts_a_clean_snapshot() -> None:
    now = datetime.now(timezone.utc)
    quotes = {
        i.symbol: Quote(i.symbol, D("85"), D("84.5"), now, Tradability.OPEN) for i in UNIVERSE
    }
    report = validate_quotes(quotes, UNIVERSE, currencies={i.symbol: "EGP" for i in UNIVERSE})
    assert report.ok, report.render()


def test_validate_quotes_flags_a_stale_bar() -> None:
    old = datetime.now(timezone.utc) - timedelta(days=30)
    instrument = UNIVERSE[0]
    quotes = {instrument.symbol: Quote(instrument.symbol, D("85"), D("84"), old)}
    report = validate_quotes(quotes, (instrument,))
    assert any(f.code == "STALE_BAR" for f in report.blocking)


def test_validate_bars_flags_duplicates_and_inverted_ranges() -> None:
    day = date(2026, 9, 21)
    series = [
        YahooRow(day, D(85), D(86), D(84), D(85)),
        YahooRow(day, D(85), D(86), D(84), D(85)),
        YahooRow(date(2026, 9, 22), D(85), D(84), D(86), D(85)),
    ]
    report = validate_bars(series, "COMI.CA")
    codes = {f.code for f in report.blocking}
    assert "DUPLICATE_DATE" in codes
    assert "INVERTED_RANGE" in codes


def test_validate_bars_flags_close_outside_range() -> None:
    series = [YahooRow(date(2026, 9, 21), D(85), D(86), D(84), D(99))]
    report = validate_bars(series, "COMI.CA")
    assert any(f.code == "CLOSE_OUTSIDE_RANGE" for f in report.blocking)


def test_validate_bars_on_empty_history_blocks() -> None:
    assert any(f.code == "EMPTY_HISTORY" for f in validate_bars([], "COMI.CA").blocking)


def test_quality_report_raise_if_blocking_is_a_no_op_when_clean() -> None:
    from egx_advisor.marketdata.base import QualityReport

    report = QualityReport()
    report.add("THIN_VOLUME", "COMI.CA", Severity.WARN, "thin")
    report.raise_if_blocking()  # must not raise on warnings alone
    assert report.ok


# ------------------------------------------------------------- json provider


async def test_json_provider_still_works(tmp_path) -> None:
    """The package move must not break the existing development path."""
    import json

    path = tmp_path / "market.json"
    path.write_text(json.dumps({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "usd_egp": "50", "usd_egp_lookback": "48",
        "quotes": {i.symbol: {"last": "85", "prev_close": "84"} for i in UNIVERSE},
    }))
    snapshot = await JsonFileMarketData(path).snapshot(UNIVERSE)
    assert snapshot.quote("COMI.CA").last == D("85")


async def test_json_provider_rejects_a_stale_file(tmp_path) -> None:
    import json

    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "as_of": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        "usd_egp": "50", "usd_egp_lookback": "48",
        "quotes": {"COMI.CA": {"last": "85", "prev_close": "84"}},
    }))
    with pytest.raises(MarketDataError, match="old"):
        await JsonFileMarketData(path).snapshot(UNIVERSE)


def test_report_deduplicates_the_same_fault_from_both_validators() -> None:
    """Counting one problem twice makes the summary read as worse than it is."""
    from egx_advisor.marketdata.base import Finding, QualityReport

    report = QualityReport()
    report.add("MISSING_SYMBOL", "EAST.CA", Severity.BLOCKING, "no rows")
    report.extend([Finding("MISSING_SYMBOL", "EAST.CA", Severity.BLOCKING, "no quote")])
    assert len(report.blocking) == 1


async def test_a_single_bad_symbol_counts_once() -> None:
    bad = rows()
    bad[-1] = YahooRow(bad[-1].day, D("42"), D("43"), D("41"), D("42"), D("100000"), "EGP")
    provider = YahooMarketData(fetch=fetch_for({"COMI.CA": bad}), enforce_quality=False)
    await provider.snapshot(UNIVERSE)
    moves = [f for f in provider.last_report.blocking if f.code == "IMPLAUSIBLE_MOVE"]
    assert len(moves) == 1
