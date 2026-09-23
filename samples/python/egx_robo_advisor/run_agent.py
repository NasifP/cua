#!/usr/bin/env python3
"""Entry point for the EGX agent daemon.

    python run_agent.py --dry-run                      # plan only, never submits
    python run_agent.py --target host --os macos       # drive this desktop
    python run_agent.py --target cloud --os linux      # drive a cua container

Targets
-------
``host`` drives the machine you are sitting at, through a locally running
``cua-computer-server``. It is the shortest path to a working desktop setup and
the riskiest one: cua controls the *whole* desktop, not a sandbox, so a
misplaced click can land on any window -- your mail, your files, another browser
tab signed into the real account. Prefer a dedicated browser profile at minimum,
and a VM if you can. ``cloud`` drives an isolated container instead, which is
slower to set up and much harder to damage anything with.

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
from dataclasses import replace
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
        "--ui",
        default="config/thndr.ui.toml",
        help="calibrated UI labels (see calibrate_ui.py). Its calibration_complete "
        "is what gates order submission.",
    )
    parser.add_argument(
        "--calibrated",
        action="store_true",
        help="force calibration_complete on, for runs without a --ui file. Prefer "
        "setting it in the file, which is a durable statement rather than a flag.",
    )
    parser.add_argument(
        "--target",
        choices=("host", "cloud"),
        default="host",
        help="'host' drives this machine via a local cua-computer-server; "
        "'cloud' drives an isolated cua container",
    )
    parser.add_argument(
        "--os",
        dest="os_type",
        choices=("macos", "linux", "windows"),
        default=None,
        help="operating system of the target (default: this machine's, for --target host)",
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

    os_type = args.os_type or _detect_os()
    if args.target == "host":
        # Targets localhost; requires `python -m computer_server` (or the
        # cua-computer-server entry point) running on this machine.
        computer = Computer(use_host_computer_server=True, os_type=os_type)
    else:
        container = os.environ.get("CUA_CONTAINER_NAME", "")
        api_key = os.environ.get("CUA_API_KEY", "")
        if not container or not api_key:
            raise SystemExit(
                "--target cloud needs CUA_CONTAINER_NAME and CUA_API_KEY. "
                "Use --target host to drive this machine instead."
            )
        computer = Computer(
            os_type=os_type, provider_type="cloud", name=container, api_key=api_key
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

    ui_path = Path(args.ui)
    if ui_path.exists():
        ui = ThndrUiMap.from_toml(ui_path)
        ui_source = str(ui_path)
    else:
        ui = ThndrUiMap()
        ui_source = "built-in placeholders (no --ui file)"
    if args.calibrated and not ui.calibration_complete:
        ui = replace(ui, calibration_complete=True)
        ui_source += " + --calibrated override"

    agent = EgxCuaAgent(
        config=AgentConfig(
            bus_path=args.bus,
            cycle_interval=args.interval,
            execute_orders=not args.dry_run,
            ui=ui,
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

    if args.target == "host":
        print(
            "!! --target host drives THIS desktop, not a sandbox. A misplaced click\n"
            "   can land on any window. Use a dedicated browser profile, or a VM.\n"
        )
    print(
        f"agent starting (target={args.target}/{os_type}, bus={args.bus}, "
        f"data={args.market_data}, "
        f"{'DRY RUN' if args.dry_run else 'EXECUTION ARMED'}, "
        f"{'calibrated' if ui.calibration_complete else 'UNCALIBRATED -- orders refused'})\n"
        f"UI labels from: {ui_source}\n"
        f"the bot is HALTED until you arm it from the dashboard"
    )
    await agent.run_forever()


def _detect_os() -> str:
    """Best guess at this machine's OS, for --target host."""
    import platform

    return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
