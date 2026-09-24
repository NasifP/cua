"""The narrow channel between the agent process and the app's Thndr X browser.

The agent never gets the browser itself. It gets this bridge, served by the app
on 127.0.0.1 behind a per-run secret, and the bridge exposes exactly three
reads: which page is open, the page's text, and a screenshot. There is no
endpoint that clicks, types or navigates, so on the read-only rung "the bot
cannot place an order" is a property of the API surface, not of a check that
something has to remember to make.

Why not the browser's own DevTools port: it grants everything -- input,
navigation, cookies, the logged-in session -- to any local process that finds
the port. This grants three reads to a caller holding the secret.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import threading
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional, Protocol

#: Environment variables the app sets for the agent it starts.
ENV_URL = "EGX_BRIDGE_URL"
ENV_SECRET = "EGX_BRIDGE_SECRET"

#: Longest page text handed on; a trading page is far below this.
MAX_TEXT = 60_000


class BridgeError(RuntimeError):
    """The bridge could not answer: app closed, page not ready, bad secret."""


class BrowserPage(Protocol):
    """What the app side must provide. Each call may block, and is thread-safe."""

    def info(self) -> dict[str, Any]: ...

    def text(self) -> str: ...

    def screenshot(self) -> bytes: ...


# --------------------------------------------------------------------------- #
# Server (runs inside the app)
# --------------------------------------------------------------------------- #


class BridgeServer:
    """Serves a BrowserPage on 127.0.0.1 at a random port, behind a secret."""

    def __init__(self, page: BrowserPage, secret: Optional[str] = None) -> None:
        self.page = page
        self.secret = secret or secrets.token_urlsafe(32)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                return

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                header = self.headers.get("Authorization", "")
                if not hmac.compare_digest(header, f"Bearer {server.secret}"):
                    self._send(401, b"unauthorised", "text/plain")
                    return
                try:
                    if self.path == "/info":
                        body = json.dumps(server.page.info()).encode()
                        self._send(200, body, "application/json")
                    elif self.path == "/text":
                        text = server.page.text()[:MAX_TEXT]
                        self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
                    elif self.path == "/screenshot":
                        self._send(200, server.page.screenshot(), "image/png")
                    else:
                        self._send(404, b"no such read", "text/plain")
                except Exception as exc:  # noqa: BLE001 - report, never crash the app
                    self._send(503, str(exc).encode("utf-8", "replace"), "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                self._send(405, b"this bridge only reads", "text/plain")

            do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "BridgeServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def env(self) -> dict[str, str]:
        return {ENV_URL: self.url, ENV_SECRET: self.secret}


# --------------------------------------------------------------------------- #
# Client (runs inside the agent)
# --------------------------------------------------------------------------- #


@dataclass
class BridgeClient:
    url: str
    secret: str
    timeout: float = 20.0
    opener: Callable[..., Any] = urllib.request.urlopen

    @classmethod
    def from_env(cls, env: Any) -> "BridgeClient":
        url, secret = env.get(ENV_URL), env.get(ENV_SECRET)
        if not url or not secret:
            raise BridgeError(
                "no browser bridge: --target browser runs inside the desktop app "
                "(desktop.cmd), which provides it"
            )
        return cls(url, secret)

    def _get(self, path: str) -> bytes:
        request = urllib.request.Request(
            self.url + path, headers={"Authorization": f"Bearer {self.secret}"}
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(f"browser bridge {path} failed: {exc}") from exc

    async def info(self) -> dict[str, Any]:
        return json.loads(await asyncio.to_thread(self._get, "/info"))

    async def text(self) -> str:
        return (await asyncio.to_thread(self._get, "/text")).decode("utf-8", "replace")

    async def screenshot(self) -> bytes:
        return await asyncio.to_thread(self._get, "/screenshot")
