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
Halting is one tap and always available. Arming takes a second press, on a
button that names the account it is about to arm. Stopping should be easier than
starting, always, and the bot never resumes itself.
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
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from egx_advisor import login_link  # noqa: E402
from egx_advisor.bus import StateBus  # noqa: E402
from egx_advisor.paths import bus_path  # noqa: E402

logger = logging.getLogger(__name__)

COOKIE_NAME = "egx_session"
MIN_TOKEN_LENGTH = 24


class ChatRequest(BaseModel):
    """A question and the conversation so far.

    History is supplied by the client and is therefore untrusted: it is clamped
    and only `user`/`assistant` roles survive, so a crafted payload cannot inject
    a `system` turn and rewrite the assistant's instructions.
    """

    question: str = ""
    history: list[dict] = []


class ResumeRequest(BaseModel):
    """JSON body for resume.

    `confirm` is the execution mode the page is currently showing, echoed back
    to the server. It is sent by the page, not typed by a person: see
    `api_resume` for what the echo is actually for.
    """

    confirm: str = ""


@dataclass(frozen=True)
class DashboardConfig:
    bus_path: str
    token: str
    #: Chat sends your holdings and their values to a model provider, so it is
    #: opt-in. The dashboard's control surface works with it off.
    chat_enabled: bool = False
    chat_model: str = "gemini/gemini-2.5-pro"

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

    @classmethod
    def from_env(cls) -> "DashboardConfig":
        return cls(
            # Anchored at the project root, so this watches the same bus as the
            # agent whatever directory either process was started from.
            bus_path=str(bus_path()),
            token=os.environ.get("EGX_DASHBOARD_TOKEN", ""),
            # Off unless asked for: enabling it sends your holdings to a model
            # provider, which should be a choice rather than a default.
            chat_enabled=os.environ.get("EGX_CHAT_ENABLED", "").lower()
            in ("1", "true", "yes"),
            chat_model=os.environ.get("EGX_CHAT_MODEL", "gemini/gemini-2.5-pro"),
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


def create_app(
    config: Optional[DashboardConfig] = None, *, assistant: Optional[Any] = None
) -> FastAPI:
    """Build the dashboard. All state is captured here, not at module scope.

    `assistant` is injected rather than constructed here so the dashboard has no
    opinion about which model answers, and so it can be left out entirely --
    chat is optional, and the control surface must work without it.
    """
    config = config or DashboardConfig.from_env()
    bus = StateBus(config.bus_path)
    if assistant is None:
        from egx_advisor.assistant import Assistant

        # With chat off the model is never called, but the opportunity scan
        # still answers, with its figures only.
        assistant = Assistant(bus=bus, model=config.chat_model, use_model=config.chat_enabled)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    app = FastAPI(title="EGX Robo-Advisor", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.bus = bus
    # The assistant reads the bus and returns text. It is given no other
    # collaborator, so there is nothing it could act through.
    app.state.assistant = assistant
    app.state.chat_busy = False

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

    def _with_session_cookie(request: Request, response: Response) -> Response:
        response.set_cookie(
            COOKIE_NAME,
            config.session_value,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, auth: str = Depends(require_session)) -> Response:
        if auth == "query":
            # Trade the URL token for an HttpOnly cookie, and move off the URL
            # that carries it, so it leaves the address bar at once.
            return _with_session_cookie(request, RedirectResponse("/", status_code=303))
        response = templates.TemplateResponse(request, "index.html", {})
        if auth == "bearer":
            _with_session_cookie(request, response)
        return response

    #: Signatures already spent, so a link from the history cannot sign in twice.
    used_login_links: set[str] = set()

    @app.get("/login")
    async def login(request: Request, ts: str = "", sig: str = "") -> Response:
        """Sign in from the launcher's short-lived link, without the password in it."""
        if sig in used_login_links or not login_link.verify(config.token, ts, sig):
            return HTMLResponse(
                "<p>This sign-in link has expired or was already used. Start the "
                "bot again with start.cmd, or open the dashboard with its password.</p>",
                status_code=401,
            )
        used_login_links.add(sig)
        return _with_session_cookie(request, RedirectResponse("/", status_code=303))

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
                "mode": _payload(snapshots, "mode"),
                "status": _payload(snapshots, "status"),
                "regime": _payload(snapshots, "regime"),
                "portfolio": _payload(snapshots, "portfolio"),
                "plan": _payload(snapshots, "plan"),
                "demo_verdict": _payload(snapshots, "demo_verdict"),
                "chat": {
                    "enabled": app.state.assistant is not None,
                    "model": (getattr(app.state.assistant, "model", "")
                              if getattr(app.state.assistant, "use_model", True) else ""),
                },
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
        """Arm the bot. Requires the page to echo back the mode it is showing.

        Stopping is one tap; starting is not. Arming puts an automated
        order-placer back in front of an account, and that must never be a
        mis-tap on a phone in a pocket. The page supplies the second step (a
        confirm press); this endpoint supplies the check that a second step
        actually happened on a page that had *seen the current mode*.

        It used to be a phrase a person typed. That phrase was a fixed string,
        so on a dashboard watching a real account it still read "RESUME
        SIMULATOR TRADING" -- asking the operator to affirm something false at
        the exact moment the screen was trying to tell them otherwise. Echoing
        the live mode cannot drift that way: the value being confirmed and the
        value on the banner come from the same place.

        Unknown mode fails closed. An absent `mode` snapshot means no agent has
        reported in, and arming a bot we cannot describe is the case this
        endpoint exists to refuse.
        """
        published = await asyncio.to_thread(bus.get, "mode")
        # get() returns {"ts": ..., "payload": ...}, not the payload itself.
        current = str(((published or {}).get("payload") or {}).get("mode") or "")
        if not current:
            raise HTTPException(
                status_code=409,
                detail=(
                    "no agent has reported its execution mode yet; start the agent "
                    "before arming it"
                ),
            )
        if not hmac.compare_digest(body.confirm.strip(), current):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"this page is showing {body.confirm.strip()!r} but the agent "
                    f"reports {current!r}; reload before arming"
                ),
            )
        actor = _actor(request)
        state = await asyncio.to_thread(
            bus.resume, actor=actor, reason="armed from the dashboard"
        )
        logger.warning("bot armed by %s", actor)
        return JSONResponse({"ok": True, "control": state.to_json()})

    @app.post("/api/chat")
    async def api_chat(
        request: Request, body: ChatRequest, auth: str = Depends(require_session)
    ) -> JSONResponse:
        """Ask the assistant about the bot.

        Explicitly not a control surface. The assistant is constructed with the
        bus and nothing else, so there is no tool it could call even if asked;
        halting and arming remain the endpoints above and the button on the page.
        """
        assistant = app.state.assistant
        if assistant is None:
            return JSONResponse(
                {"answer": "Chat is disabled on this dashboard."}, status_code=503
            )

        # One in flight at a time. A chat panel should not be able to queue up
        # model calls faster than they complete.
        if app.state.chat_busy:
            return JSONResponse(
                {"answer": "Still answering the previous question."}, status_code=429
            )
        app.state.chat_busy = True
        try:
            answer = await asyncio.to_thread(
                assistant.answer, body.question, body.history
            )
        finally:
            app.state.chat_busy = False
        return JSONResponse({"answer": answer})

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


def _load_env_file() -> None:
    """Read .env into the environment, if one is there.

    Called from `main()`, never at import: this module promises no import-time
    side effects, and a test importing it must not pick up a developer's .env.
    Real environment variables win over the file.
    """
    from egx_advisor.settings import load_secrets_into_environ

    load_secrets_into_environ()
    from egx_advisor.paths import PROJECT_ROOT

    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            "%s exists but python-dotenv is not installed, so it was ignored. "
            "Install it, or set EGX_DASHBOARD_TOKEN in your shell.",
            env_path,
        )
        return
    load_dotenv(env_path, override=False)
    logger.info("loaded %s", env_path)


def main() -> None:
    """Entry point. Refuses a public bind unless explicitly acknowledged."""
    import uvicorn

    from egx_advisor.parent_watch import start_from_env

    logging.basicConfig(level=logging.INFO)
    start_from_env()  # stop, halted, if the app that started us is gone
    _load_env_file()
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
