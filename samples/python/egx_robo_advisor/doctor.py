#!/usr/bin/env python3
"""Check that this machine can run the bot, and print the fix for anything that cannot.

    .venv\\Scripts\\python.exe doctor.py      (Windows)
    .venv/bin/python doctor.py              (macOS / Linux)

Read-only: it changes nothing. See egx_advisor/doctor.py for the checks.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from egx_advisor.doctor import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
