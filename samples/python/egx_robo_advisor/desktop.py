#!/usr/bin/env python3
"""The desktop app: dashboard, chat and Thndr X in one window.

On Windows, double-click desktop.cmd instead. See egx_advisor/desktop/app.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    try:
        from egx_advisor.desktop.app import main
    except ImportError as exc:
        raise SystemExit(
            f"the desktop app needs PySide6 ({exc}). Run install.cmd again, or:\n"
            f'  .venv\\Scripts\\python.exe -m pip install -e ".[desktop]"'
        ) from exc
    raise SystemExit(main())
