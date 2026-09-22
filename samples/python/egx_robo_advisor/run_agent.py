#!/usr/bin/env python3
"""Entry point for the EGX agent daemon.

    python run_agent.py --dry-run          # plan only, never submits an order
    python run_agent.py                    # arm the execution path

The bot always starts HALTED. Arming is done from the dashboard, deliberately:
starting an automated order-placer should be a conscious act with a typed
confirmation, not a side effect of running a command.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.egx_cua_agent import AgentConfig, EgxCuaAgent  # noqa: E402
from egx_advisor.execution.thndr import ThndrUiMap  # noqa: E402
from egx_advisor.marketdata import JsonFileMarketData, YahooMarketData  # noqa: E402
from egx_advisor.safety.demo_guard import DemoGuard  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EGX robo-advisor agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and publish, but never submit an order",
    )
    parser.add_argument(
        "--calibrated",
        action="store_true",
        help="assert that ThndrUiMap labels have been verified against the live app; "
        "order submission is refused without it",
    )
    parser.add_argument("--bus", default=os.environ.get("EGX_BUS_PATH", "state/egx_bus.db"))
    parser.add_argument(
        "--market-data",
        default="yahoo",
        help="'yahoo' for the live provider, or a path to a JSON snapshot file",
    )
    parser.add_argument("--interval", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        from computer import Computer
        from cua_agent import ComputerAgent
    except ImportError as exc:
        raise SystemExit(
            f"cua is not installed: {exc}\n"
            f"Install the agent extra: pip install -e '.[agent]'"
        ) from exc

    computer = Computer(
        os_type="android",  # Thndr is a mobile app; see README for the desktop-web path
        provider_type="cloud",
        name=os.environ.get("CUA_CONTAINER_NAME", ""),
        api_key=os.environ.get("CUA_API_KEY", ""),
    )

    def agent_factory(guarded_computer: object) -> ComputerAgent:
        # Note the argument: the *guarded* computer, never the raw one. This is
        # what keeps model-chosen clicks inside the safety gates.
        return ComputerAgent(
            model="anthropic/claude-sonnet-5",
            tools=[guarded_computer],
            only_n_most_recent_images=3,
            trajectory_dir="trajectories",
            max_trajectory_budget={"max_budget": 5.0, "raise_error": True},
        )

    agent = EgxCuaAgent(
        config=AgentConfig(
            bus_path=args.bus,
            cycle_interval=args.interval,
            execute_orders=not args.dry_run,
            ui=ThndrUiMap(calibration_complete=args.calibrated),
        ),
        computer=computer,
        market_data=(
            YahooMarketData()
            if args.market_data == "yahoo"
            else JsonFileMarketData(Path(args.market_data))
        ),
        agent_factory=agent_factory,
        demo_guard=DemoGuard.default(),
    )
    agent.install_signal_handlers()

    print(
        f"agent starting (bus={args.bus}, data={args.market_data}, "
        f"{'DRY RUN' if args.dry_run else 'EXECUTION ARMED'}, "
        f"{'calibrated' if args.calibrated else 'UNCALIBRATED -- orders refused'})\n"
        f"the bot is HALTED until you arm it from the dashboard"
    )
    await agent.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
