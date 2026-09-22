#!/usr/bin/env python3
"""Check whether a market-data source is fit to size real orders.

    python verify_market_data.py                 # live check against Yahoo
    python verify_market_data.py --history 1825  # also check a long series

This exists because "the API returned 200" is not the same as "these prices are
correct for the Egyptian Exchange". Yahoo's EGX coverage is unofficial and
delayed, and nobody -- including the author of this file -- can tell you from
memory how good it currently is for a given ticker. So the script prints what it
actually received, in a form you can hold next to the exchange's own page, and
runs every automated check that can be made without a second source.

What it CANNOT do
-----------------
It cannot confirm the prices are right. Only you can, by comparing the closes
below against egx.com.eg or your broker for a few sessions. The automated checks
catch faults that are self-evident from the data -- wrong currency, unadjusted
splits, stale bars, gaps -- not a feed that is quietly wrong by 2%.

Run this before ever passing --calibrated to the agent, and again whenever the
universe changes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.clock import TradingCalendar  # noqa: E402
from egx_advisor.marketdata import (  # noqa: E402
    DataQualityError,
    MarketDataError,
    QualityPolicy,
    YahooMarketData,
)
from egx_advisor.marketdata.yahoo import USD_EGP_SYMBOL  # noqa: E402
from egx_advisor.strategy.policy import AllocationPolicy  # noqa: E402


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="verify market data quality")
    parser.add_argument("--history", type=int, default=0,
                        help="also pull this many days of history and validate it")
    parser.add_argument("--lookback", type=int, default=30)
    parser.add_argument("--allow-blocking", action="store_true",
                        help="report findings without raising, to see everything at once")
    args = parser.parse_args()

    policy = AllocationPolicy()
    universe = policy.universe
    provider = YahooMarketData(
        quality=QualityPolicy(),
        lookback_days=args.lookback,
        enforce_quality=not args.allow_blocking,
    )

    rule("UNIVERSE")
    for instrument in universe:
        print(f"  {instrument.symbol:10} {instrument.sleeve.value:16} {instrument.name}")
    print(f"  {USD_EGP_SYMBOL:10} {'macro':16} USD/EGP (drives the devaluation switch)")

    rule("LIVE SNAPSHOT")
    try:
        snapshot = await provider.snapshot(universe)
    except DataQualityError as exc:
        print(f"  BLOCKED: {exc}\n")
        print(exc.report.render())
        print("\n  The provider refused to return a snapshot. That is the intended")
        print("  behaviour: bad prices size bad orders. Re-run with --allow-blocking")
        print("  to see every finding at once.")
        return 2
    except MarketDataError as exc:
        print(f"  FAILED: {exc}")
        print("\n  If this is an import error, install the extra:")
        print("    pip install -e '.[marketdata]'")
        print("  If it is a network error, Yahoo may be unreachable from here.")
        return 1

    print(f"  as of {snapshot.as_of.isoformat()}")
    print(f"  {'symbol':10} {'last':>12} {'prev close':>12} {'move':>9}  state")
    for instrument in universe:
        quote = snapshot.quote(instrument.symbol)
        if quote is None:
            print(f"  {instrument.symbol:10} {'-- no quote --':>35}")
            continue
        print(
            f"  {quote.symbol:10} {quote.last:>12} {quote.prev_close:>12} "
            f"{quote.move_from_prev_close:>+8.2%}  {quote.tradability.value}"
        )

    print(f"\n  USD/EGP now          {snapshot.usd_egp}")
    print(f"  USD/EGP lookback     {snapshot.usd_egp_lookback}")
    print(f"  trailing depreciation {snapshot.egp_depreciation:+.2%}")
    state = policy.describe(snapshot.egp_depreciation)
    print(f"  implied policy state  {state}")
    if snapshot.usd_egp == snapshot.usd_egp_lookback == Decimal("1"):
        print("\n  !! No USD/EGP series. The devaluation switch cannot fire, so the")
        print(f"     policy is pinned to baseline. Verify that '{USD_EGP_SYMBOL}' resolves.")

    rule("AUTOMATED QUALITY CHECKS")
    report = provider.last_report
    if report is None:
        print("  no report produced")
    else:
        print(report.render())
        print(f"\n  {len(report.blocking)} blocking, {len(report.warnings)} warning(s)")

    if args.history:
        rule(f"HISTORY ({args.history} days)")
        try:
            rows = await provider.history(universe, days=args.history)
        except DataQualityError as exc:
            print(f"  BLOCKED: {exc}\n")
            print(exc.report.render())
            return 2
        except MarketDataError as exc:
            print(f"  FAILED: {exc}")
            return 1
        calendar = TradingCalendar()
        for instrument in universe:
            series = sorted(rows.get(instrument.symbol) or (), key=lambda r: r.day)
            if not series:
                print(f"  {instrument.symbol:10} NO HISTORY")
                continue
            span = (series[-1].day - series[0].day).days
            expected = sum(
                1 for i in range(span + 1)
                if calendar.is_session_day(series[0].day + timedelta(days=i))
            )
            coverage = len(series) / expected if expected else 0.0
            flag = "" if coverage > 0.9 else "   <-- sparse"
            print(
                f"  {instrument.symbol:10} {len(series):>5} rows  "
                f"{series[0].day} .. {series[-1].day}  coverage {coverage:>5.0%}{flag}"
            )

    rule("WHAT YOU MUST STILL DO BY HAND")
    print("  These checks cannot be automated from a single source:")
    print()
    print("  1. Open egx.com.eg (or your broker) and compare the closes above for")
    print("     COMI.CA and AZG.CA across three or four sessions. A feed that is")
    print("     quietly wrong by a couple of percent passes every check above.")
    print("  2. Confirm the quotes are in EGP, not piastres or USD.")
    print("  3. Confirm AZG.CA is the gold ETF you actually intend to hold.")
    print("  4. Note the delay. Yahoo is not real time; if the lag is material for")
    print("     your limit prices, this source is fine for backtests and unfit for")
    print("     live sizing.")
    print()
    print("  Until all four pass, keep the agent on --dry-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
