"""What the Sprint 4 widgets need from the app, in one place, callable off the UI thread.

Every method here may block (SQLite, Yahoo, the dashboard's chat, the Thndr X
channel), so widgets call them only through workers.run_async. Nothing here
touches a widget.

- `toolbox()`: the analyst's tools (analyst/tools.py) with the app's price
  archive and the Thndr X channel, so a card's "Prepare order" runs exactly
  the analyst's prepare_buy, with all its checks.
- `browse()`: the same BrowseServer channel the dashboard's analyst uses.
- `ask_team()`: the dashboard's own chat endpoint, so an Omnibar question goes
  through the committee, its reserved budget and its buy gate like any chat
  question, and the lead's decision lands in "team_views" for the cards.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

#: The committee's worst case is a few minutes; the chat endpoint answers one
#: question at a time.
ASK_TIMEOUT = 240.0


@dataclass
class Services:
    env: Mapping[str, str]
    dashboard_port: int
    memory: Any
    archive: Any
    #: The BrowseServer's env (URL and secret); {} before it has started.
    browse_env: Callable[[], Mapping[str, str]] = dict
    opener: Callable[..., Any] = urllib.request.urlopen

    # ------------------------------------------------------------------ bus

    def bus(self) -> Any:
        """A StateBus for this thread. Callers close it (use `with`)."""
        from ..bus import StateBus
        from ..paths import bus_path

        return StateBus(bus_path())

    # ------------------------------------------------------------------ tools

    def browse(self) -> Optional[Any]:
        from ..browse import BrowseClient

        return BrowseClient.from_env(self.browse_env())

    def toolbox(self, bus: Any) -> Any:
        from .. import levels
        from ..analyst.tools import Toolbox, yahoo_history, yahoo_usd_egp

        return Toolbox(bus=bus, memory=self.memory, archive=self.archive,
                       history=yahoo_history(self.archive), style=levels.style_from(self.env),
                       env=self.env, browser=self.browse(), usd_egp=yahoo_usd_egp)

    # ------------------------------------------------------------------ the team

    def ask_team(self, question: str) -> str:
        """Ask the dashboard's chat (the committee when it is on); its answer."""
        token = self.env.get("EGX_DASHBOARD_TOKEN") or ""
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.dashboard_port}/api/chat",
            data=json.dumps({"question": question, "history": []}).encode("utf-8"),
            method="POST", headers={"Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=ASK_TIMEOUT) as response:
                return str(json.loads(response.read().decode("utf-8")).get("answer") or "")
        except urllib.error.HTTPError as exc:
            # 429: another question is being answered; 503: chat is off.
            try:
                return str(json.loads(exc.read().decode("utf-8")).get("answer") or exc)
            except Exception:  # noqa: BLE001 - no JSON body: the HTTP error itself
                return str(exc)
