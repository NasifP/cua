"""A child halts and exits when the app that started it is gone."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from egx_advisor import parent_watch


def test_this_process_is_alive_and_a_finished_one_is_not():
    assert parent_watch.alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not parent_watch.alive(child.pid)
    assert not parent_watch.alive(0)


def test_the_watch_fires_once_the_parent_is_gone():
    gone = threading.Event()
    answers = iter([True, True, False])
    thread = parent_watch.watch(123, on_gone=gone.set, is_alive=lambda _pid: next(answers),
                                interval=0.01)
    thread.join(timeout=2)
    assert gone.is_set()


def test_no_parent_id_means_no_watch():
    assert parent_watch.start_from_env({}) is None
    assert parent_watch.start_from_env({"EGX_PARENT_PID": "abc"}) is None


def test_a_killed_parent_takes_the_child_down(tmp_path):
    """End to end: parent starts a watching child, parent is killed, child exits."""
    script = tmp_path / "child.py"
    marker = tmp_path / "halted"
    script.write_text(
        "import sys, time, threading\n"
        f"sys.path.insert(0, {str(os.getcwd())!r})\n"
        "from egx_advisor import parent_watch\n"
        f"parent_watch.watch(int(sys.argv[1]), on_gone=lambda: (open({str(marker)!r}, 'w').close(),"
        " __import__('os')._exit(0)), interval=0.05)\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child = subprocess.Popen([sys.executable, str(script), str(parent.pid)])
    parent.kill()
    parent.wait()
    assert child.wait(timeout=10) == 0
    assert marker.exists()
