#!/usr/bin/env python3
"""Backtest runner.

    python run_backtest.py --synthetic                  # exercise the engine
    python run_backtest.py --yahoo --days 1825          # real data, cached
    python run_backtest.py --csv prices.csv --macro fx.csv

What it reports, and why in this order
--------------------------------------
The headline return is deliberately not the headline. For a rebalancer the
questions worth answering are:

  1. **What do the frictions cost?**  `policy` vs `frictionless` isolates
     commission, duty and slippage from everything else.
  2. **Is rebalancing worth doing at all?**  `policy` vs `buy & hold` asks
     whether correcting drift beats leaving the portfolio alone once costs are
     paid. If it does not, the bands or the turnover cap are wrong.
  3. **Could the difference plausibly be zero?**  Every comparison carries a
     block-bootstrap interval, and any interval straddling zero is reported as
     indistinguishable from noise.

This tool does not rank parameter values and must not be used to pick them.
See docs/NO_OVERFIT_CHARTER.md.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.backtest import (  # noqa: E402
    SYNTHETIC_WARNING,
    BacktestConfig,
    CostModel,
    compare,
    compute,
    fetch_history,
    load_csv,
    run_backtest,
    run_buy_and_hold,
    sample_size_warning,
    synthetic_history,
)
from egx_advisor.marketdata import (  # noqa: E402
    DataQualityError,
    MarketDataError,
    YahooMarketData,
)
from egx_advisor.strategy.policy import AllocationPolicy  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EGX robo-advisor backtester")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="OHLCV history: date,symbol,open,high,low,close[,volume]")
    source.add_argument("--synthetic", action="store_true",
                        help="generate random prices to exercise the engine (NOT a result)")
    source.add_argument("--yahoo", action="store_true",
                        help="pull real history through the same provider the bot trades on")
    p.add_argument("--macro", help="date,usd_egp series for the devaluation switch")
    p.add_argument("--sessions", type=int, default=750, help="synthetic only")
    p.add_argument("--days", type=int, default=1825, help="--yahoo: calendar days of history")
    p.add_argument("--cache", default="state/history.csv", help="--yahoo: CSV cache path")
    p.add_argument("--refresh", action="store_true", help="--yahoo: ignore the cache")
    p.add_argument("--min-coverage", type=float, default=0.80,
                   help="--yahoo: flag symbols below this share of sessions")
    p.add_argument("--seed", type=int, default=20260922, help="synthetic only")
    p.add_argument("--start-cash", type=Decimal, default=Decimal("1000000"))
    p.add_argument("--commission", type=Decimal, default=None,
                   help="commission rate, e.g. 0.002 (confirm with your broker)")
    p.add_argument("--slippage", type=Decimal, default=None, help="slippage rate, e.g. 0.001")
    return p.parse_args()


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    args = parse_args()
    policy = AllocationPolicy()
    symbols = [i.symbol for i in policy.universe]

    costs = CostModel()
    if args.commission is not None:
        costs = CostModel(
            commission_rate=args.commission,
            commission_min=costs.commission_min,
            exchange_fee_rate=costs.exchange_fee_rate,
            stamp_duty_rate=costs.stamp_duty_rate,
            slippage_rate=args.slippage if args.slippage is not None else costs.slippage_rate,
        )
    elif args.slippage is not None:
        costs = CostModel(slippage_rate=args.slippage)

    if args.synthetic:
        print(f"\n!! {SYNTHETIC_WARNING}\n")
        history = synthetic_history(
            symbols, start=date(2022, 1, 2), sessions=args.sessions, seed=args.seed,
            devaluation_at=args.sessions // 2,
        )
    elif args.yahoo:
        provider = YahooMarketData()
        try:
            history, coverage, quality = asyncio.run(
                fetch_history(
                    provider, policy.universe, days=args.days,
                    cache=args.cache, refresh=args.refresh,
                    min_coverage=args.min_coverage,
                )
            )
        except DataQualityError as exc:
            print(f"BLOCKED: {exc}\n", file=sys.stderr)
            print(exc.report.render(), file=sys.stderr)
            print("\nA backtest on bad data produces a number that looks like evidence.",
                  file=sys.stderr)
            return 2
        except MarketDataError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            if "not installed" in str(exc):
                print("\nInstall the extra:  pip install -e '.[marketdata]'", file=sys.stderr)
            else:
                print("\nThis looks like a connectivity or ticker problem rather than a",
                      file=sys.stderr)
                print("missing package. Run verify_market_data.py to see what the feed",
                      file=sys.stderr)
                print("actually returns.", file=sys.stderr)
            return 1

        rule("DATA COVERAGE")
        print(coverage.render())
        if quality is not None and quality.warnings:
            print()
            for finding in quality.warnings:
                print(finding.render())
        if coverage.unusable:
            print(f"\n  !! {sorted(coverage.unusable)} cover less than "
                  f"{args.min_coverage:.0%} of sessions.")
            print("     They are still held, because dropping a universe member changes")
            print("     the policy's target weights -- that is your decision, not a side")
            print("     effect of a patchy download. But results involving them are weak.")
    else:
        history = load_csv(args.csv, macro_path=args.macro, symbols=symbols)
        missing = set(symbols) - history.symbols()
        if missing:
            print(f"warning: no bars for {sorted(missing)}; they cannot be held", file=sys.stderr)

    base = dict(policy=policy, starting_cash=args.start_cash)
    priced = run_backtest(history, BacktestConfig(**base, costs=costs, label="policy"))
    free = run_backtest(
        history, BacktestConfig(**base, costs=CostModel.frictionless(), label="frictionless")
    )
    hold = run_buy_and_hold(history, BacktestConfig(**base, costs=costs, label="policy"))

    rule("RESULTS")
    for result in (priced, free, hold):
        print(compute(result).render())
        print()

    rule("WHAT THE FRICTIONS COST")
    print(compare(priced, free).render())
    print()
    print("  The gap above IS the cost of trading: commission, duty and slippage.")
    print("  It is the most reliable number in this report, because it does not")
    print("  depend on the price path being representative.")

    rule("IS REBALANCING WORTH IT?")
    print(compare(priced, hold).render())
    print()
    priced_m, hold_m = compute(priced), compute(hold)
    print(f"  drift from target:  policy {priced_m.mean_drift:.2%}  vs  "
          f"buy & hold {hold_m.mean_drift:.2%}")
    print(f"  cost of that:       {priced_m.total_costs:,.0f} EGP "
          f"({priced_m.cost_drag:.2%} of starting value)")
    print(f"  fill rate:          {priced_m.fill_rate:.0%} "
          f"({priced_m.rejections} orders the market did not take)")

    warning = sample_size_warning(priced_m.days)
    if warning:
        rule("SAMPLE SIZE")
        print(f"  {warning}")

    rule("READ THIS BEFORE ACTING ON ANY NUMBER ABOVE")
    print("  This tool measures frictions. It does not choose parameters, and the")
    print("  admissible-value check in PolicyParameters will reject a fitted one.")
    print("  A 'conclusive' verdict on one sample path is still one sample path.")
    if args.synthetic:
        print(f"\n  !! {SYNTHETIC_WARNING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
