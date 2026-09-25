"""Start the computer server, the dashboard and the agent together; stop them together.

    start.cmd                   (Windows: double-click)
    .venv/bin/python launch.py  (macOS / Linux)

Replaces three terminals that each had to stay open, each needed the venv
activated, and failed with "only one usage of each socket address" when a
second copy was started. What it keeps:

- The dashboard is still its own process. The kill switch must work when the
  agent is wedged, which is the whole reason they were split (ADR-001).
- The bot still starts halted and still needs the two-press arm. The launcher
  never arms anything.
- Stopping halts the bus *before* any process is terminated, so an agent
  killed mid-cycle leaves the bot halted rather than armed.

A server that is already running and answers its health route is reused
rather than started twice. Each child's output goes to state/logs/<name>.log.
"""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .doctor import (
    COMPUTER_SERVER_PORT,
    FAIL,
    Check,
    _probe_port,
    check_api_keys,
    check_mode,
    check_packages,
    check_python,
    check_tesseract,
    check_token,
    render,
)
from .login_link import make_login_path
from .paths import PROJECT_ROOT, bus_path, child_command

LOG_DIR = PROJECT_ROOT / "state" / "logs"
MAX_RESTARTS = 3


@dataclass
class ProcessSpec:
    name: str
    argv: list[str]
    #: Port and health route that identify an already-running copy we can reuse.
    port: Optional[int] = None
    health: str = ""
    #: Restart if it exits unexpectedly.
    restart: bool = True


@dataclass
class Child:
    spec: ProcessSpec
    process: Any
    restarts: int = 0
    log: Any = None


@dataclass
class Launcher:
    specs: list[ProcessSpec]
    env: Mapping[str, str]
    popen: Callable[..., Any] = subprocess.Popen
    probe: Callable[[int, str], str] = _probe_port
    halt: Callable[[str], None] = field(default=lambda reason: _halt_bus(reason))
    say: Callable[[str], None] = print
    children: list[Child] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)

    def start_all(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        for spec in self.specs:
            if spec.port is not None:
                state = self.probe(spec.port, spec.health)
                if state == "ours":
                    self.say(f"  {spec.name}: already running on port {spec.port}, reusing it")
                    self.reused.append(spec.name)
                    continue
                if state == "other":
                    raise SystemExit(
                        f"port {spec.port} is used by another program, so {spec.name} "
                        f"cannot start. Find it with: Get-NetTCPConnection -LocalPort "
                        f"{spec.port}"
                    )
            self.children.append(self._spawn(spec))

    def _spawn(self, spec: ProcessSpec, restarts: int = 0) -> Child:
        log = open(LOG_DIR / f"{spec.name}.log", "a", encoding="utf-8")  # noqa: SIM115
        kwargs: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            # The child halts the bus and exits if this process disappears.
            "env": {**self.env, "EGX_PARENT_PID": str(os.getpid())},
            "stdout": log,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            # Own process group: Ctrl+C reaches only the launcher, which halts
            # the bus before it stops anything.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        process = self.popen(spec.argv, **kwargs)
        self.say(f"  {spec.name}: started (log: state/logs/{spec.name}.log)")
        return Child(spec, process, restarts, log)

    def supervise_once(self) -> None:
        """Restart any child that exited, up to MAX_RESTARTS each."""
        for index, child in enumerate(self.children):
            code = child.process.poll()
            if code is None:
                continue
            if child.log:
                child.log.close()
            if not child.spec.restart or child.restarts >= MAX_RESTARTS:
                self.say(
                    f"  {child.spec.name} stopped (exit {code}) and will not be "
                    f"restarted; see state/logs/{child.spec.name}.log"
                )
                child.spec.restart = False
                continue
            self.say(f"  {child.spec.name} exited (code {code}); restarting")
            self.children[index] = self._spawn(child.spec, child.restarts + 1)

    def stop_all(self) -> None:
        """Halt first, then stop the agent, then everything else."""
        self.halt("launcher stopped")
        self.say("  bot halted")
        for child in sorted(self.children, key=lambda c: c.spec.name != "agent"):
            if child.process.poll() is None:
                child.process.terminate()
        deadline = time.monotonic() + 10
        for child in self.children:
            try:
                child.process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001
                child.process.kill()
            if child.log:
                child.log.close()


def _halt_bus(reason: str, actor: str = "launcher") -> None:
    from .bus import StateBus

    with StateBus(bus_path()) as bus:
        bus.halt(actor=actor, reason=reason)


def _os_name() -> str:
    return {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")


def build_specs(env: Mapping[str, str], python: str = sys.executable) -> list[ProcessSpec]:
    dashboard_port = int(env.get("EGX_DASHBOARD_PORT", "8787"))
    return [
        ProcessSpec(
            "computer-server", [python, "-m", "computer_server"],
            port=COMPUTER_SERVER_PORT, health="/status",
        ),
        ProcessSpec(
            "dashboard", child_command("dashboard", executable=python),
            port=dashboard_port, health="/healthz",
        ),
        # --dry-run always: order submission is refused until calibration is
        # complete anyway, and the launcher is not the place to change that.
        ProcessSpec(
            "agent",
            child_command("agent", "--target", "host", "--os", _os_name(), "--dry-run",
                          executable=python),
        ),
    ]


def preflight(env: Mapping[str, str]) -> list[Check]:
    """The doctor checks that would stop a start, run before anything is spawned."""
    checks = [check_python(), check_packages(), check_token(env), check_mode(env)]
    checks += check_api_keys(env)
    checks.append(check_tesseract(env=env))
    return checks


def wait_until_up(probe: Callable[[int, str], str], port: int, path: str,
                  timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(port, path) == "ours":
            return True
        time.sleep(0.5)
    return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    from .doctor import load_env

    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    print("EGX Robo-Advisor\n")
    checks = preflight(env)
    if any(c.status == FAIL for c in checks):
        print(render([c for c in checks if c.status != "ok"]))
        print("\nNothing was started. Fix the lines above, then start again.")
        return 1

    launcher = Launcher(specs=build_specs(env), env=dict(os.environ))
    launcher.start_all()

    port = int(env.get("EGX_DASHBOARD_PORT", "8787"))
    if not wait_until_up(launcher.probe, port, "/healthz"):
        print("  the dashboard did not come up; see state/logs/dashboard.log")
    else:
        # A two-minute, single-use link: the password itself never enters the
        # browser's history.
        link = make_login_path(env["EGX_DASHBOARD_TOKEN"])
        webbrowser.open(f"http://127.0.0.1:{port}{link}")
        print(f"\n  Dashboard opened in your browser (http://127.0.0.1:{port}).")
    print(
        "\n  The bot is HALTED. To start it:\n"
        "    1. open Thndr X in its own window, on the Positions tab\n"
        "    2. press START THE BOT, then CONFIRM, on the dashboard\n"
        "    3. switch to Thndr X within the countdown, and leave the mouse alone\n"
        "\n  Press Ctrl+C here to halt the bot and stop everything.\n"
    )

    stop = False

    def _request_stop(*_: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _request_stop)  # type: ignore[attr-defined]
    try:
        while not stop:
            launcher.supervise_once()
            time.sleep(1.0)
    finally:
        print("\n  stopping ...")
        launcher.stop_all()
        print("  stopped. The bot is halted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
