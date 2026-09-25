"""Children stop when the app that started them is gone.

The desktop app and the launcher start the dashboard and the agent as
separate processes. When the app closes normally it halts the bus and stops
them. When it does not -- killed from Task Manager, crashed, power button on
the terminal -- they used to keep running: an agent with no window, and a
dashboard holding port 8787 so the next start could not open.

Each child now watches the process that started it (EGX_PARENT_PID). When
that process is gone, the child halts the bus -- the safe state, and what a
normal close does -- and exits.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Mapping, Optional

ENV_KEY = "EGX_PARENT_PID"
INTERVAL_SECONDS = 2.0


def alive(pid: int) -> bool:
    """Whether a process with this id is running. Never signals it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # os.kill(pid, 0) would *terminate* the process on Windows.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        synchronize, still_active = 0x00100000 | 0x1000, 259  # + PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _halt_and_exit() -> None:
    try:
        from .launcher import _halt_bus

        _halt_bus("the app that started this process is gone", actor="parent watch")
    finally:
        os._exit(0)


def watch(
    pid: int,
    on_gone: Callable[[], None] = _halt_and_exit,
    is_alive: Callable[[int], bool] = alive,
    interval: float = INTERVAL_SECONDS,
) -> threading.Thread:
    def loop() -> None:
        while is_alive(pid):
            time.sleep(interval)
        on_gone()

    thread = threading.Thread(target=loop, name="parent-watch", daemon=True)
    thread.start()
    return thread


def start_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[threading.Thread]:
    """Watch EGX_PARENT_PID if it is set. A process started by hand has none."""
    value = (os.environ if env is None else env).get(ENV_KEY, "")
    try:
        pid = int(value)
    except ValueError:
        return None
    return watch(pid) if pid > 0 else None
