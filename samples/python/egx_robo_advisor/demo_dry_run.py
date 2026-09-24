#!/usr/bin/env python3
"""Dry-run harness: exercises the real pipeline without a broker or a screen.

    python demo_dry_run.py

Why this exists alongside `run_agent.py --dry-run`: that flag still reads the
portfolio off a live screen, so it needs Cua credentials and a device. This
harness substitutes a scripted screen and a fixture price file, and runs
everything else for real -- the same `NewsFetcher`, `KeywordClassifier`,
`RegimeFilter`, `plan_rebalance`, `GuardedInterface` and `StateBus` the daemon
uses. It is the honest way to read the bot's reasoning before pointing it at
anything that can place an order.

IMPORTANT: every price, holding and headline below is an ILLUSTRATIVE FIXTURE,
not market data. The numbers are plausible in shape so the frictions are visible;
they are not quotes, and nothing here is investment advice.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.bus import StateBus
from egx_advisor.marketdata import JsonFileMarketData
from egx_advisor.regime.filter import RegimeFilter
from egx_advisor.regime.sentiment import KeywordClassifier
from egx_advisor.safety.demo_guard import DemoGuard, DemoModeViolation
from egx_advisor.safety.guarded_interface import GuardedInterface, KillSwitchEngaged
from egx_advisor.safety.pngutil import encode_png
from egx_advisor.strategy.policy import AllocationPolicy
from egx_advisor.strategy.rebalance import plan_rebalance
from egx_advisor.types import Headline, Portfolio, Position, Side

BLANK = encode_png(8, 8, bytes(8 * 8 * 3))
POLICY = AllocationPolicy()


# --------------------------------------------------------------------------- #
# Illustrative fixtures -- NOT market data
# --------------------------------------------------------------------------- #

def market_file(tmp: Path, *, usd_egp: str, lookback: str, overrides: dict | None = None) -> Path:
    quotes = {
        "COMI.CA": {"last": "85.40", "prev_close": "84.90"},
        "TMGH.CA": {"last": "52.10", "prev_close": "51.75"},
        "SWDY.CA": {"last": "78.30", "prev_close": "77.60"},
        "ABUK.CA": {"last": "66.20", "prev_close": "66.80"},
        "EAST.CA": {"last": "40.15", "prev_close": "40.05"},
        "ETEL.CA": {"last": "35.60", "prev_close": "35.80"},
        "AZG.CA": {"last": "19.05", "prev_close": "18.95"},
    }
    quotes.update(overrides or {})
    path = tmp / "market.json"
    path.write_text(
        json.dumps(
            {
                "as_of": datetime.now(timezone.utc).isoformat(),
                "usd_egp": usd_egp,
                "usd_egp_lookback": lookback,
                "quotes": quotes,
            }
        )
    )
    return path


def drifted_portfolio() -> Portfolio:
    """Moderately drifted: the bank a bit heavy, the hedge far too light, cash idle.

    Deliberately *not* extreme. A severely drifted book (say the bank at 59%)
    makes every scenario look identical, because the 15% turnover cap is entirely
    consumed by the one big corrective sell and no buy survives to demonstrate
    anything. That is correct behaviour -- see the convergence note at the end of
    the run -- but it hides the other mechanisms, so the fixture is sized to let
    both a sell and a buy through in one cycle.
    """
    now = datetime.now(timezone.utc)
    return Portfolio(
        as_of=now,
        positions={
            "COMI.CA": Position("COMI.CA", D("1150"), D("98210")),   # ~16% vs 10%
            "TMGH.CA": Position("TMGH.CA", D("1180"), D("61478")),   # ~10%, in band
            "AZG.CA": Position("AZG.CA", D("1200"), D("22860")),     # ~3.7% vs 30%
        },
        cash_egp=D("430000"),
        unsettled_cash_egp=D("35000"),
        demo_confirmed=True,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show_plan(plan, allowed=None, suppressed=()) -> None:
    total = sum((p.market_value for p in PORTFOLIO.positions.values()), D(0)) + PORTFOLIO.cash_egp
    print(f"portfolio value : {total:,.0f} EGP")
    print(f"policy state    : {plan.policy_state}")
    if plan.notes:
        for note in plan.notes:
            print(f"note            : {note}")

    print("\n  drift (held -> target)")
    for symbol in sorted(plan.target_weights):
        target = plan.target_weights[symbol]
        held = plan.current_weights.get(symbol, D(0))
        drift = target - held
        flag = "  <-- outside band" if abs(drift) >= POLICY.params.name_drift_band else ""
        print(f"    {symbol:9} {held:7.2%} -> {target:7.2%}   ({drift:+.2%}){flag}")

    orders = plan.orders if allowed is None else allowed
    print(f"\n  ORDERS ({len(orders)})")
    if not orders:
        print("    (none)")
    for o in orders:
        print(f"    {o.side.value.upper():4} {o.symbol:9} {o.quantity:>8} @ {o.limit_price:>8} "
              f"= {o.notional:>10,.0f} EGP")
        print(f"         why: {o.rationale}")

    if suppressed:
        print(f"\n  SUPPRESSED BY REGIME FILTER ({len(suppressed)})")
        for o, reason in suppressed:
            print(f"    {o.side.value.upper():4} {o.symbol:9} <- {reason}")

    if plan.skipped:
        print(f"\n  SKIPPED BY STRATEGY ({len(plan.skipped)})")
        for s in plan.skipped:
            print(f"    {s.side.value.upper():4} {s.symbol:9} <- {s.reason}")

    print(f"\n  turnover {plan.turnover_egp:,.0f} EGP "
          f"(cap {POLICY.params.max_turnover_per_cycle * total:,.0f})")


PORTFOLIO = drifted_portfolio()


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

async def scenario_calm(tmp: Path) -> None:
    rule("1. CALM MARKET, CLEAN FEEDS  (EGP stable -> baseline 60/30/10)")
    market = await JsonFileMarketData(market_file(tmp, usd_egp="50.20", lookback="49.10")).snapshot(
        POLICY.universe
    )
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)
    regime = RegimeFilter().evaluate([], now=datetime.now(timezone.utc), feed_age_seconds=120)
    allowed, suppressed = RegimeFilter().apply(plan.orders, regime)
    print(f"regime          : {regime.risk_state.value.upper()} -- {regime.drivers[0]}")
    show_plan(plan, allowed, suppressed)


async def scenario_devaluation(tmp: Path) -> None:
    rule("2. EGP DEVALUED ~29%  (trigger 15% -> devaluation-stress 45/45/10)")
    market = await JsonFileMarketData(market_file(tmp, usd_egp="62.00", lookback="48.00")).snapshot(
        POLICY.universe
    )
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)
    print("regime          : (news clean; this is the STRATEGY reacting, not the news layer)")
    show_plan(plan)


async def scenario_frictions(tmp: Path) -> None:
    rule("3. EGX FRICTIONS  (one halted name, one printing at its daily limit)")
    market = await JsonFileMarketData(
        market_file(
            tmp,
            usd_egp="50.20",
            lookback="49.10",
            overrides={
                "EAST.CA": {"last": "40.15", "prev_close": "40.05", "tradability": "halted"},
                "SWDY.CA": {"last": "85.30", "prev_close": "77.60"},  # +9.9%, limit band
            },
        )
    ).snapshot(POLICY.universe)
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)
    show_plan(plan)


async def scenario_catastrophic_news(tmp: Path) -> None:
    rule("4. CATASTROPHIC NEWS  (circuit breaker: buys halted, SELLS SURVIVE)")
    market = await JsonFileMarketData(market_file(tmp, usd_egp="50.20", lookback="49.10")).snapshot(
        POLICY.universe
    )
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)

    now = datetime.now(timezone.utc)
    headlines = [
        Headline("CBE", "Central bank devalues the Egyptian pound by 20%", "u1", now),
        Headline("EGX", "EGX halts trading in EAST.CA pending disclosure", "u2", now),
    ]
    assessments = KeywordClassifier().classify(headlines)
    for a in assessments:
        print(f"  classified      : {a.severity.value:13} {a.scope.value:7} "
              f"{str(a.symbols):14} {a.headline.title[:52]}")

    rf = RegimeFilter()
    regime = rf.evaluate(assessments, now=now, feed_age_seconds=60)
    print(f"\nregime          : {regime.risk_state.value.upper()}")
    for d in regime.drivers[:3]:
        print(f"  driver        : {d[:100]}")
    allowed, suppressed = rf.apply(plan.orders, regime)
    show_plan(plan, allowed, suppressed)


async def scenario_good_news(tmp: Path) -> None:
    rule("5. BULLISH NEWS  (must change NOTHING -- no pump chasing)")
    market = await JsonFileMarketData(market_file(tmp, usd_egp="50.20", lookback="49.10")).snapshot(
        POLICY.universe
    )
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)
    now = datetime.now(timezone.utc)
    bullish = KeywordClassifier().classify([
        Headline("Press", "COMI.CA smashes earnings, shares surge 20%", "g1", now),
        Headline("Press", "EGX30 hits an all-time high on record foreign inflows", "g2", now),
    ])
    for a in bullish:
        print(f"  classified      : {a.severity.value:13} <- {a.headline.title[:60]}")
    rf = RegimeFilter()
    regime = rf.evaluate(bullish, now=now, feed_age_seconds=60)
    allowed, suppressed = rf.apply(plan.orders, regime)
    print(f"\nregime          : {regime.risk_state.value.upper()}")
    print(f"plan unchanged  : {tuple(allowed) == tuple(plan.orders)}  "
          f"(suppressed: {len(suppressed)})")


async def scenario_feeds_down(tmp: Path) -> None:
    rule("6. FEEDS UNREACHABLE  (fail-closed: silence is not calm)")
    market = await JsonFileMarketData(market_file(tmp, usd_egp="50.20", lookback="49.10")).snapshot(
        POLICY.universe
    )
    plan = plan_rebalance(portfolio=PORTFOLIO, market=market, policy=POLICY)
    rf = RegimeFilter()
    regime = rf.evaluate(
        [],
        now=datetime.now(timezone.utc),
        feed_age_seconds=None,
        sources_failed=("EGX disclosures", "Mubasher Egypt", "Enterprise MEA"),
        in_session=True,
    )
    print(f"regime          : {regime.risk_state.value.upper()}  (stale={regime.stale})")
    for d in regime.drivers:
        print(f"  driver        : {d[:100]}")
    allowed, suppressed = rf.apply(plan.orders, regime)
    print(f"\nbuys allowed    : {sum(1 for o in allowed if o.side is Side.BUY)}")
    print(f"sells allowed   : {sum(1 for o in allowed if o.side is Side.SELL)}")


# --------------------------------------------------------------------------- #
# Safety layer against a scripted screen
# --------------------------------------------------------------------------- #

class ScriptedScreen:
    """Stands in for the Thndr UI. Records what actually reached it."""

    def __init__(self, labels):
        self.labels = list(labels)
        self.calls: list[str] = []

    @property
    def tree(self):
        return {"children": [{"label": x} for x in self.labels]}

    async def screenshot(self):
        return BLANK

    async def left_click(self, x, y):
        self.calls.append(f"left_click({x},{y})")

    async def type_text(self, t):
        self.calls.append(f"type_text({t!r})")


async def scenario_safety(tmp: Path) -> None:
    rule("7. SAFETY LAYER vs a scripted screen")
    # Every bus is closed before main() removes the temporary directory:
    # Windows will not delete a database file SQLite still holds open.
    with ExitStack() as buses:
        bus = buses.enter_context(StateBus(tmp / "demo_bus.db"))
        bus.resume(actor="demo", reason="armed for the dry run")

        screen = ScriptedScreen(["Simulator", "Portfolio", "Real Estate Sector"])
        gi = GuardedInterface(
            screen, bus=bus,
            guard=DemoGuard(text_probes=(), colour_probe=None),
            accessibility_tree_provider=lambda: screen.tree,
        )

        print("  a) simulator badge present, plus a 'Real Estate' label (the trap)")
        async with gi.order_critical("BUY COMI.CA x600"):
            await gi.type_text("600")
            await gi.left_click(412, 880)
        print(f"     -> reached screen: {screen.calls}")

        print("\n  b) account flips to the REAL account mid-session")
        screen.labels = ["Real Account", "Portfolio"]   # the switch the guard must catch
        before = len(screen.calls)
        try:
            await gi.left_click(1, 1)
            print("     -> !! FAILURE: a click landed on a live account")
        except DemoModeViolation as exc:
            print(f"     -> blocked: {str(exc)[:88]}")
        print(f"     -> clicks that landed: {len(screen.calls) - before}")
        print(f"     -> bot auto-halted: {bus.is_halted()} ({bus.control_state().reason[:46]})")

        print("\n  c) kill switch pressed from the dashboard process")
        bus2 = buses.enter_context(StateBus(tmp / "demo_bus2.db"))
        bus2.resume(actor="demo", reason="armed")
        screen2 = ScriptedScreen(["Simulator"])
        gi2 = GuardedInterface(
            screen2, bus=bus2,
            guard=DemoGuard(text_probes=(), colour_probe=None),
            accessibility_tree_provider=lambda: screen2.tree,
        )
        await gi2.left_click(5, 5)
        # A second handle on the same file, standing in for the dashboard process.
        dashboard = buses.enter_context(StateBus(tmp / "demo_bus2.db"))
        dashboard.halt(actor="mobile:pola", reason="KILL SWITCH from phone")
        try:
            await gi2.left_click(6, 6)
            print("     -> !! FAILURE: click got through after the kill switch")
        except KillSwitchEngaged as exc:
            print(f"     -> blocked: {exc}")
        print(f"     -> clicks that landed: {screen2.calls}")

        print("\n  d) what the dashboard would show (journal tail)")
        for e in bus.recent_events(limit=9):
            print(f"     {e.ts.strftime('%H:%M:%S')}  {e.kind:9} {e.message[:76]}")


async def main() -> None:
    print(__doc__)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await scenario_calm(tmp)
        await scenario_devaluation(tmp)
        await scenario_frictions(tmp)
        await scenario_catastrophic_news(tmp)
        await scenario_good_news(tmp)
        await scenario_feeds_down(tmp)
        await scenario_safety(tmp)
    rule("NOTE ON CONVERGENCE")
    print(
        "The turnover cap is 15% of portfolio value per cycle, so a badly drifted\n"
        "book does not snap to target in one session -- it walks there over several.\n"
        "A holding at 59% against a 10% target needs roughly five or six sessions.\n"
        "That is the intended behaviour (gradual, low market impact, no single large\n"
        "print), but it is worth knowing before you watch the first day's plan and\n"
        "wonder why the portfolio still looks wrong at the close."
    )
    rule("END -- no order was placed; no real account was touched")


if __name__ == "__main__":
    asyncio.run(main())
