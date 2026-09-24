#!/usr/bin/env python3
"""Start the computer server, the dashboard and the agent in one go.

On Windows, double-click start.cmd instead. Ctrl+C halts the bot and stops all
three. See egx_advisor/launcher.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.cua_runtime import require_cua_python  # noqa: E402

if __name__ == "__main__":
    require_cua_python(Path(__file__).resolve().parent)
    from egx_advisor.launcher import main

    raise SystemExit(main())
