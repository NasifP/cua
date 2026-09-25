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

The bot always starts HALTED, on every start. Arming is done from the
dashboard, with two presses, the second naming the account: starting an
automated order-placer should be a conscious act, not a side effect of running
a command. On Windows, start.cmd runs this together with the dashboard.
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

from egx_advisor.cua_runtime import require_cua_python  # noqa: E402
from egx_advisor.egx_cua_agent import AgentConfig, EgxCuaAgent  # noqa: E402
from egx_advisor.execution.thndr import ThndrUiMap  # noqa: E402
from egx_advisor.marketdata import JsonFileMarketData, YahooMarketData  # noqa: E402
from egx_advisor.paths import bus_path, ui_map_path  # noqa: E402
from egx_advisor.safety.demo_guard import DemoGuard  # noqa: E402
from egx_advisor.safety.modes import ExecutionMode  # noqa: E402
from egx_advisor.strategy.four_factor import FourFactorParams  # noqa: E402


def _load_env_file() -> str:
    """Read .env into the environment, if one is there and python-dotenv is installed.

    Called from `main()` rather than at import, because this module promises to
    have no import-time side effects -- a test that imports it must not pick up
    whatever happens to be in the developer's .env.

    Real environment variables win over the file: an operator who exports a
    token for one run should not have it silently overridden by a stale file.
    """
    # Keys saved from the Settings page live in the OS credential store, not in
    # .env; fill them first so .env's blank placeholders cannot hide them.
    from egx_advisor.settings import load_secrets_into_environ

    load_secrets_into_environ()
    from egx_advisor.paths import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
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
        default=str(ui_map_path()),
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
        choices=("host", "cloud", "browser"),
        default="host",
        help="'host' drives this machine via a local cua-computer-server; "
        "'cloud' drives an isolated cua container; 'browser' reads Thndr X from "
        "the desktop app's own browser (started by desktop.cmd)",
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
        # EGX_MODE in .env sets this, so the launcher and a bare
        # `python run_agent.py` agree on which account is being driven.
        default=os.environ.get("EGX_MODE") or ExecutionMode.SIMULATOR_ONLY.value,
        help="what the bot may do, and on whose account (see safety/modes.py)",
    )
    # Resolved after .env is loaded, against the project root, so the agent and
    # the dashboard always share one bus whatever directory each started in.
    parser.add_argument("--bus", default=None)
    parser.add_argument(
        "--market-data",
        default="yahoo",
        help="'yahoo' for the live provider, or a path to a JSON snapshot file",
    )
    parser.add_argument(
        "--interval", type=float,
        default=max(60.0, float(os.environ.get("EGX_CYCLE_SECONDS") or 300.0)),
        help="seconds between cycles while the market is open (EGX_CYCLE_SECONDS)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


async def main() -> None:
    from egx_advisor.parent_watch import start_from_env

    start_from_env()  # stop, halted, if the app that started us is gone
    # Before parse_args: argument defaults read the environment.
    env_note = _load_env_file()
    args = parse_args()
    if args.bus is None:
        args.bus = str(bus_path())
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    os_type = args.os_type or _detect_os()
    mode = ExecutionMode.parse(args.mode)
    computer = None
    agent_factory = None
    executor_factory = None
    if args.target == "browser":
        # No cua on this path: the desktop app owns the browser, and the agent
        # reaches it only through the app's read-only bridge.
        from egx_advisor.desktop.bridge import BridgeClient, BridgeError
        from egx_advisor.execution.browser import BrowserExecutor

        try:
            bridge = BridgeClient.from_env(os.environ)
        except BridgeError as exc:
            raise SystemExit(str(exc)) from exc

        def executor_factory(bus):  # noqa: F811 - replaces the None above
            return BrowserExecutor(bridge=bridge, bus=bus, mode=mode, ui=ui)

    # Before the import: on an unsupported Python, pip installs an old cua that
    # imports fine and then fails later on something that does not mention
    # Python at all.
    if args.target != "browser":
        require_cua_python(Path(__file__).resolve().parent)
        try:
            from computer import Computer
            from cua_agent import ComputerAgent
        except ImportError as exc:
            raise SystemExit(
                f"cua is not installed: {exc}\n"
                f"Install the agent extra: pip install -e '.[agent]'"
            ) from exc

    if args.target == "browser":
        pass
    elif args.target == "host":
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

    def cua_agent_factory(guarded_computer: object) -> "ComputerAgent":
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

    if args.target != "browser":
        agent_factory = cua_agent_factory
    agent = EgxCuaAgent(
        config=AgentConfig(
            bus_path=args.bus,
            cycle_interval=args.interval,
            execute_orders=not args.dry_run,
            mode=mode,
            ui=ui,
            four_factor=(
                FourFactorParams()
                if os.environ.get("EGX_FOUR_FACTOR", "").strip().lower() == "true"
                else None
            ),
        ),
        computer=computer,
        market_data=(
            YahooMarketData()
            if args.market_data == "yahoo"
            else JsonFileMarketData(Path(args.market_data))
        ),
        agent_factory=agent_factory,
        demo_guard=DemoGuard.default(),
        executor_factory=executor_factory,
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
            "   The bot reads a real account and publishes real plans. It sends no\n"
            "   input at all: every click, key and typed character is refused at\n"
            + (
                "   the app's bridge, which only reads. Keep the app's Thndr X tab\n"
                "   on Positions.\n"
                if args.target == "browser"
                else "   the guard. Leave Thndr X in front on the Positions tab.\n"
            )
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
