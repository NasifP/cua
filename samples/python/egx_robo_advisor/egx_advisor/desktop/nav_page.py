"""The Thndr X view as `browse.Page`: info, run a script, load an address.

Called from the browse server's threads. Qt objects belong to the GUI thread,
so each call is handed to it through a queued signal and the caller waits on
a Future, the same way the read-only bridge reads the page (app.QtPage).

Only browse.py's operations call this, with its own scripts: the server that
fronts it offers read, open, click (checked), search, open_stock and prepare,
never "run this script".
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtWebEngineCore import QWebEngineScript

TIMEOUT = 20.0


class QtNavPage(QObject):
    _request = Signal(str, object, object)

    def __init__(self, view: Callable[[], Any]) -> None:
        super().__init__()
        self._view_of = view
        self._request.connect(self._handle, Qt.QueuedConnection)

    def _call(self, kind: str, arg: Any = None) -> Any:
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("QtNavPage calls must come from a server thread")
        future: Future = Future()
        self._request.emit(kind, arg, future)
        return future.result(timeout=TIMEOUT)

    def info(self) -> dict[str, Any]:
        return self._call("info")

    def run(self, script: str) -> Any:
        return self._call("run", script)

    def load(self, url: str) -> None:
        self._call("load", url)

    @Slot(str, object, object)
    def _handle(self, kind: str, arg: Any, future: Future) -> None:
        try:
            view = self._view_of()
            if kind == "info":
                future.set_result({"url": view.url().toString(), "title": view.title(),
                                   "loading": bool(getattr(view, "egx_loading", False))})
            elif kind == "run":
                # The app's isolated world: the page's scripts cannot see or
                # change what runs here.
                view.page().runJavaScript(arg, QWebEngineScript.ApplicationWorld,
                                          lambda result: future.set_result(result))
            elif kind == "load":
                view.egx_loading = True
                view.load(QUrl(arg))
                future.set_result(None)
            else:
                future.set_exception(ValueError(kind))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
