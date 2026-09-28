"""Slow work off the UI thread, results back on it.

Every model call, bus read, price fetch and Thndr X operation started from a
widget goes through `run_async`: the function runs on a small thread pool and
its result (or error) comes back through a Qt signal. The relay object lives
on the UI thread, so Qt queues the signal and the callback runs there: widgets
are only ever touched from the UI thread, and the Thndr X view never waits on
a network call.

Thndr X operations are special: `browse.Browser` drives the page by posting
to the UI thread and waiting (nav_page.QtNavPage). It must therefore never be
called *on* the UI thread, or it would wait for itself. Going through the
pool (and the BrowseServer's HTTP channel) keeps that true.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

#: Four at once: the Omnibar starts opinion, news and Thndr X together.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="egx-ui")


class _Relay(QObject):
    """Created on the UI thread; its signal carries results back to it."""

    done = Signal(object, object, object)  # callback, result, error

    @Slot(object, object, object)
    def deliver(self, callback: Callable[..., None], result: Any,
                error: Optional[BaseException]) -> None:
        try:
            callback(result, error)
        except Exception:  # noqa: BLE001 - one bad callback must not break the UI
            logger.exception("UI callback failed")


_relay: Optional[_Relay] = None


def _get_relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()
        _relay.done.connect(_relay.deliver)
    return _relay


def run_async(fn: Callable[[], Any], on_done: Callable[[Any, Optional[BaseException]], None]
              ) -> None:
    """Run `fn` off the UI thread; call `on_done(result, error)` on the UI thread.

    Call from the UI thread (the relay is created there on first use).
    """
    relay = _get_relay()

    def job() -> None:
        try:
            result, error = fn(), None
        except Exception as exc:  # noqa: BLE001 - handed to the callback
            result, error = None, exc
        # Emitted from a pool thread to a UI-thread object: Qt queues it.
        relay.done.emit(on_done, result, error)

    _POOL.submit(job)


def shutdown() -> None:
    """Stop taking work; running jobs finish (their results are dropped)."""
    _POOL.shutdown(wait=False, cancel_futures=True)
