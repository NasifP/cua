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
from egx_advisor.safety.modes import ExecutionMode  # noqa: E402


def _load_env_file() -> str:
    """Read .env into the environment, if one is there and python-dotenv is installed.

    Called from `main()` rather than at import, because this module promises to
    have no import-time side effects -- a test that imports it must not pick up
    whatever happens to be in the developer's .env.

    Real environment variables win over the file: an operator who exports a
    token for one run should not have it silently overridden by a stale file.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return ""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return (
            f"{env_path} exists but python-dotenv is not installed, so it was "
            f"ignored. Install it, or set the variables in your shell."
        )
    load_dotenv(env_path, override=False)
    return f"loaded {env_path}"


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
    parser.add_argument(
        "--mode",
        choices=[m.value for m in ExecutionMode],
        default=ExecutionMode.SIMULATOR_ONLY.value,
        help="what the bot may do, and on whose account (see safety/modes.py)",
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
    env_note = _load_env_file()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        from computer import Computer
        from cua_agent import ComputerAgent
    except ImportError as exc:
        if sys.version_info >= (3, 14):
            # pip on 3.14 installs cua-agent 0.5.x, the last release without an
            # upper Python bound. It imports as `agent`, so "not installed" would
            # send the operator to reinstall the same wrong version.
            raise SystemExit(
                f"cua does not support Python {sys.version_info.major}."
                f"{sys.version_info.minor} yet ({exc}).\n"
                f"Use Python 3.12 or 3.13, then: pip install -e '.[agent]'"
            ) from exc
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
            # Unlike the classifier and the chat assistant, this one drives a
            # screen, so it needs a model with computer-use support -- not every
            # capable model has it. Check your provider before switching, and
            # expect navigation to fail loudly rather than subtly if it lacks it.
            model=os.environ.get("EGX_AGENT_MODEL", "anthropic/claude-sonnet-5"),
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

    mode = ExecutionMode.parse(args.mode)
    agent = EgxCuaAgent(
        config=AgentConfig(
            bus_path=args.bus,
            cycle_interval=args.interval,
            execute_orders=not args.dry_run,
            mode=mode,
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
    if mode.permits_live_account and not mode.permits_order_tickets:
        print(
            f"!! {mode.banner}\n"
            "   The bot will look at a real account and publish real plans.\n"
            "   It cannot open an order ticket: the guard refuses every\n"
            "   order-critical primitive in this mode.\n"
        )
    elif mode.permits_live_account:
        print(
            f"!! {mode.banner}\n"
            "   The bot will FILL ORDER TICKETS on a real account and stop.\n"
            "   It never submits: Enter and the submit button's own rectangle\n"
            "   are refused at the guard. You press submit yourself, or you\n"
            "   discard the ticket.\n"
            "   Watch the screen while it runs. A fence stops the click the bot\n"
            "   aims at the button it was told about; it cannot stop one aimed\n"
            "   somewhere the layout moved to.\n"
        )
    print(
        f"agent starting ({mode.banner}, target={args.target}/{os_type}, bus={args.bus}, "
        f"data={args.market_data}, "
        f"{'DRY RUN' if args.dry_run else 'EXECUTION ARMED'}, "
        f"{'calibrated' if ui.calibration_complete else 'UNCALIBRATED -- orders refused'})\n"
        f"UI labels from: {ui_source}\n"
        + (f"env: {env_note}\n" if env_note else "")
        + "the bot is HALTED until you arm it from the dashboard"
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
