"""Short-lived, single-use sign-in links for the dashboard.

The launcher used to open `/?token=<password>`, which left the dashboard's
long-lived password in the browser's history. It now opens `/login?ts=..&sig=..`
instead: an HMAC of a timestamp under the password, valid for two minutes and
accepted once. The history keeps a spent link, not the password.

The launcher and the dashboard are separate processes that share nothing but
the password, so the link is derived from it rather than stored anywhere.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlencode

LINK_LIFETIME_SECONDS = 120
_CONTEXT = b"egx-dashboard-login-v1:"


def _signature(token: str, ts: int) -> str:
    return hmac.new(token.encode(), _CONTEXT + str(ts).encode(), hashlib.sha256).hexdigest()


def make_login_path(token: str, now: Optional[float] = None) -> str:
    ts = int(now if now is not None else time.time())
    return "/login?" + urlencode({"ts": ts, "sig": _signature(token, ts)})


def verify(token: str, ts: str, sig: str, now: Optional[float] = None) -> bool:
    """True if the link was made with this password in the last two minutes."""
    try:
        issued = int(ts)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if not 0 <= current - issued <= LINK_LIFETIME_SECONDS:
        return False
    return hmac.compare_digest(sig or "", _signature(token, issued))
