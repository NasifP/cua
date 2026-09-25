#!/usr/bin/env python3
"""Entry point of the packaged Windows app (EGX Robo-Advisor.exe).

One executable plays three parts. Started plainly it is the desktop window.
The window then starts itself twice more, as it would start `python
dashboard/app.py` and `python run_agent.py` from a checkout:

    "EGX Robo-Advisor.exe"                    the desktop app
    "EGX Robo-Advisor.exe" --role dashboard   the dashboard server
    "EGX Robo-Advisor.exe" --role agent ...   the agent (arguments follow)

From a checkout, run desktop.py as before; this file also works there.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _role(argv: list[str]) -> tuple[str, list[str]]:
    if len(argv) >= 2 and argv[0] == "--role":
        return argv[1], argv[2:]
    return "desktop", argv


def _keep_output(name: str) -> None:
    """A windowed app has no console: print() goes nowhere, and a crash leaves
    no trace. Send both to a log file when there is nowhere else for them."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from egx_advisor.paths import PROJECT_ROOT

    logs = PROJECT_ROOT / "state" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stream = open(logs / f"{name}.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


#: Loaded lazily by the app, so a missing one would only show up in use.
SELFCHECK_MODULES = (
    "egx_advisor.desktop.app", "dashboard.app", "run_agent", "PySide6.QtWebEngineWidgets",
    "PySide6.QtSvg", "litellm", "yfinance", "keyring", "uvicorn", "fastapi", "jinja2", "dotenv",
    "egx_advisor.execution.browser", "egx_advisor.backtest.lab",
)


def selfcheck(report: Path) -> int:
    """Import what the app loads lazily and check its bundled data. Used by the
    build: a packaged app that cannot find litellm fails here, not in use."""
    import importlib
    import traceback

    lines, failed = [], 0

    def record(name: str, check) -> None:
        nonlocal failed
        try:
            detail = check()
            lines.append(f"ok    {name}" + (f"  ({detail})" if detail else ""))
        except Exception:  # noqa: BLE001 - every failure is reported, none stops the run
            failed += 1
            lines.append(f"FAIL  {name}\n" + traceback.format_exc(limit=3))

    for module in SELFCHECK_MODULES:
        record(module, lambda m=module: importlib.import_module(m) and "")

    def cairo_time():
        from zoneinfo import ZoneInfo

        ZoneInfo("Africa/Cairo")

    def model_prices():
        import litellm

        if "gemini/gemini-2.5-pro" not in litellm.model_cost:
            raise LookupError("litellm's model price table is missing")
        return f"{len(litellm.model_cost)} models priced"

    def shipped_files():
        from egx_advisor.paths import RESOURCE_ROOT

        for rel in ("dashboard/templates/index.html", ".env.example", "config/policy.egx.toml"):
            if not (RESOURCE_ROOT / rel).is_file():
                raise FileNotFoundError(rel)

    def key_store():
        import os

        import keyring

        store = keyring.get_keyring()
        name = type(store).__name__
        # On Windows the keys belong in Credential Manager. A build that lost
        # its backend would quietly fall back to writing them into .env.
        if os.name == "nt" and getattr(type(store), "priority", 0) <= 0:
            raise RuntimeError(f"no usable key store in the build ({name})")
        return name

    record("time zone Africa/Cairo", cairo_time)
    record("litellm model prices", model_prices)
    record("shipped files", shipped_files)
    record("key store", key_store)
    lines.append("PASSED" if not failed else f"{failed} FAILED")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    role, rest = _role(argv)
    from egx_advisor.paths import FROZEN, bootstrap_data_root

    if FROZEN:
        bootstrap_data_root()
    _keep_output(role)
    if role == "selfcheck":
        return selfcheck(Path(rest[0]) if rest else Path("selfcheck.txt"))
    if role == "dashboard":
        from dashboard.app import main as dashboard_main

        dashboard_main()
        return 0
    if role == "agent":
        import asyncio

        import run_agent

        sys.argv = ["run_agent.py", *rest]
        try:
            asyncio.run(run_agent.main())
        except KeyboardInterrupt:
            pass
        return 0
    if role != "desktop":
        print(f"unknown role {role!r}", file=sys.stderr)
        return 2
    from egx_advisor.desktop.app import main as desktop_main

    return desktop_main([sys.argv[0], *rest])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
