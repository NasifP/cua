"""Mobile dashboard: read the bot, and stop the bot.

Runs as its own process and shares nothing with the agent except the bus file.
That separation is the point: the kill switch must be pressable when the agent
loop is wedged mid-click, and a button living inside the stuck process cannot be
pressed. See `egx_advisor/bus.py`.

The app is built by `create_app(config)` rather than assembled at import time, so
importing this module has no side effects -- no database opened, no environment
read, no exception raised in a tool that merely wanted to inspect it.

Exposure
--------
This binds to 127.0.0.1 by default. It is a remote control for something that
places orders, so putting it on a public interface directly would be a poor idea
even with a token. The intended path is a tunnel that terminates TLS and
authenticates in front of this app -- Cloudflare Tunnel or Tailscale -- with
`EGX_DASHBOARD_HOST=0.0.0.0` only inside that private network. The app refuses to
start without a token, and refuses a non-loopback bind unless you have
acknowledged it explicitly, because "I'll add auth later" is how a trading bot
ends up reachable from the open internet.

Asymmetric friction
-------------------
Halting is one tap and always available. Resuming requires a typed confirmation
phrase. Stopping should be easier than starting, always, and the bot never
resumes itself.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from egx_advisor.bus import StateBus  # noqa: E402

logger = logging.getLogger(__name__)

COOKIE_NAME = "egx_session"
MIN_TOKEN_LENGTH = 24


class ResumeRequest(BaseModel):
    """JSON body for resume.

    JSON rather than a form: `Form(...)` drags in python-multipart for no benefit
    when the only field is a short string.
    """

    confirm: str = ""


@dataclass(frozen=True)
class DashboardConfig:
    bus_path: str
    token: str
    resume_phrase: str = "RESUME SIMULATOR TRADING"

    def __post_init__(self) -> None:
        if not self.token:
            raise RuntimeError(
                "EGX_DASHBOARD_TOKEN is not set. This app can halt and resume a bot "
                "that places orders; it will not start without a token."
            )
        if len(self.token) < MIN_TOKEN_LENGTH:
            raise RuntimeError(
                f"EGX_DASHBOARD_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
            )
        if not self.resume_phrase.strip():
            raise RuntimeError("EGX_RESUME_PHRASE must not be empty")

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        return cls(
            bus_path=os.environ.get("EGX_BUS_PATH", "state/egx_bus.db"),
            token=os.environ.get("EGX_DASHBOARD_TOKEN", ""),
            resume_phrase=os.environ.get("EGX_RESUME_PHRASE", "RESUME SIMULATOR TRADING"),
        )

    @property
    def session_value(self) -> str:
        """Cookie value derived from the token.

        Derived rather than equal to it, so a leaked cookie cannot be replayed as
        a bearer token, and so sessions survive a restart with no shared store.
        """
        return hmac.new(
            self.token.encode(), b"egx-dashboard-session-v1", "sha256"
        ).hexdigest()


def create_app(config: Optional[DashboardConfig] = None) -> FastAPI:
    """Build the dashboard. All state is captured here, not at module scope."""
    config = config or DashboardConfig.from_env()
    bus = StateBus(config.bus_path)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    app = FastAPI(title="EGX Robo-Advisor", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.bus = bus

    # ----------------------------------------------------------------- auth

    def valid(candidate: Optional[str], expected: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate or "", expected)

    async def require_session(
        request: Request, egx_session: Optional[str] = Cookie(default=None)
    ) -> str:
        if valid(egx_session, config.session_value):
            return "cookie"
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer ") and valid(header[7:].strip(), config.token):
            return "bearer"
        if valid(request.query_params.get("token"), config.token):
            return "query"
        raise HTTPException(status_code=401, detail="unauthorised")

    # ---------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, auth: str = Depends(require_session)) -> Response:
        response = templates.TemplateResponse(
            request, "index.html", {"resume_phrase": config.resume_phrase}
        )
        if auth in ("bearer", "query"):
            # Trade the URL token for an HttpOnly cookie so it stops appearing in
            # browser history, referrers, and the phone's address bar.
            response.set_cookie(
                COOKIE_NAME,
                config.session_value,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                max_age=60 * 60 * 24 * 30,
            )
        return response

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Unauthenticated liveness only. Says nothing about the portfolio."""
        return JSONResponse({"ok": True, "ts": _now()})

    # ---------------------------------------------------------------- state

    @app.get("/api/state")
    async def api_state(auth: str = Depends(require_session)) -> JSONResponse:
        """Everything the dashboard renders, in one round trip."""
        snapshots, control, events = await asyncio.gather(
            asyncio.to_thread(bus.all_snapshots),
            asyncio.to_thread(bus.control_state),
            asyncio.to_thread(bus.recent_events, limit=60),
        )
        return JSONResponse(
            {
                "ts": _now(),
                "control": control.to_json(),
                "status": _payload(snapshots, "status"),
                "regime": _payload(snapshots, "regime"),
                "portfolio": _payload(snapshots, "portfolio"),
                "plan": _payload(snapshots, "plan"),
                "demo_verdict": _payload(snapshots, "demo_verdict"),
                "events": [e.to_json() for e in events],
                "latest_seq": events[-1].seq if events else 0,
            }
        )

    @app.get("/api/events/stream")
    async def api_events_stream(
        request: Request, after: int = 0, auth: str = Depends(require_session)
    ) -> StreamingResponse:
        """Server-sent events: the agent's live click log.

        SSE rather than a websocket because the flow is one-directional and SSE
        reconnects on its own -- which matters when the client is a phone moving
        between cell and wifi.
        """

        async def generate() -> AsyncIterator[bytes]:
            cursor = after
            yield b"retry: 3000\n\n"  # reconnect hint for the browser
            while True:
                if await request.is_disconnected():
                    break
                events = await asyncio.to_thread(bus.events_since, cursor, limit=100)
                for event in events:
                    cursor = event.seq
                    payload = json.dumps(event.to_json(), ensure_ascii=False)
                    yield f"id: {event.seq}\nevent: agent\ndata: {payload}\n\n".encode()
                control = await asyncio.to_thread(bus.control_state)
                yield f"event: control\ndata: {json.dumps(control.to_json())}\n\n".encode()
                await asyncio.sleep(1.0)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -------------------------------------------------------------- control

    @app.post("/api/control/halt")
    async def api_halt(
        request: Request, auth: str = Depends(require_session)
    ) -> JSONResponse:
        """The kill switch. One tap, no confirmation, idempotent.

        No confirmation dialog on purpose. If someone is reaching for this, they
        want the bot stopped now, and a modal between them and that is a
        liability. Halting an already-halted bot is harmless.
        """
        actor = _actor(request)
        state = await asyncio.to_thread(
            bus.halt, actor=actor, reason="KILL SWITCH pressed from the dashboard"
        )
        logger.warning("kill switch engaged by %s", actor)
        return JSONResponse({"ok": True, "control": state.to_json()})

    @app.post("/api/control/resume")
    async def api_resume(
        request: Request,
        body: ResumeRequest,
        auth: str = Depends(require_session),
    ) -> JSONResponse:
        """Arm the bot. Requires the exact phrase typed out.

        Deliberately more effort than halting. Resuming puts an automated
        order-placer back in front of an account, and that should never be a
        mis-tap on a phone in a pocket.
        """
        if not hmac.compare_digest(body.confirm.strip(), config.resume_phrase):
            raise HTTPException(
                status_code=400,
                detail=f"type the exact phrase to resume: {config.resume_phrase!r}",
            )
        actor = _actor(request)
        state = await asyncio.to_thread(
            bus.resume, actor=actor, reason="armed from the dashboard"
        )
        logger.warning("bot armed by %s", actor)
        return JSONResponse({"ok": True, "control": state.to_json()})

    @app.get("/api/control/audit")
    async def api_audit(auth: str = Depends(require_session)) -> JSONResponse:
        rows = await asyncio.to_thread(bus.control_audit, limit=50)
        return JSONResponse({"audit": list(rows)})

    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _payload(snapshots: dict[str, Any], key: str) -> Any:
    entry = snapshots.get(key)
    return entry["payload"] if entry else None


def _actor(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    agent = request.headers.get("user-agent", "")
    kind = "mobile" if any(t in agent.lower() for t in ("iphone", "android", "mobile")) else "web"
    return f"{kind}:{client}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    """Entry point. Refuses a public bind unless explicitly acknowledged."""
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("EGX_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("EGX_DASHBOARD_PORT", "8787"))
    if host not in ("127.0.0.1", "::1", "localhost") and os.environ.get(
        "EGX_ACKNOWLEDGE_PUBLIC_BIND"
    ) != "yes":
        raise RuntimeError(
            f"refusing to bind {host}: this app can halt and arm a trading bot. "
            f"Put it behind a tunnel that terminates TLS and authenticates, then set "
            f"EGX_ACKNOWLEDGE_PUBLIC_BIND=yes if you still want a non-loopback bind."
        )
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
