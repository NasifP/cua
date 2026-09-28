"""Proactive alerts: a holding at its stop or first target, and the news brake.

`check` compares each holding's latest price with its stop and first target
(levels.py) and looks at the news layer and MACRO_RISK_OFF (market_state.py).
It returns the alerts not sent yet today; `Ledger` keeps what was sent on the
bus, so a restart does not repeat them and two windows do not both alert.

The price is the one the agent last read from Thndr X (the portfolio
snapshot, refreshed every cycle while the market is open), so an alert is as
fresh as that read. Nothing here sells: an alert tells the operator, who acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

KEY = "alerts_sent"
STOP, TARGET1, BRAKE = "stop", "target1", "brake"


@dataclass(frozen=True)
class Alert:
    kind: str
    ticker: str
    price: Optional[float]
    level: Optional[float]
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.ticker or '*'}"


def _payload(snapshots: Mapping[str, Any], key: str) -> Any:
    entry = snapshots.get(key)
    return (entry or {}).get("payload") if isinstance(entry, Mapping) else None


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def check(snapshots: Mapping[str, Any]) -> list[Alert]:
    """Every alert the current state calls for (not yet de-duplicated)."""
    from .browse import ticker_of

    out: list[Alert] = []
    levels_snap = _payload(snapshots, "levels") or {}
    levels = levels_snap.get("levels") or {}
    for p in (_payload(snapshots, "portfolio") or {}).get("positions") or ():
        symbol = str(p.get("symbol") or "")
        lv = levels.get(symbol) or {}
        qty, value = _num(p.get("quantity")), _num(p.get("market_value"))
        stop, target1 = _num(lv.get("stop")), _num(lv.get("target1"))
        if not (qty and value) or not (stop or target1):
            continue
        price = round(value / qty, 3)
        ticker = ticker_of(symbol)
        if stop and price <= stop:
            out.append(Alert(STOP, ticker, price, stop))
        elif target1 and price >= target1:
            out.append(Alert(TARGET1, ticker, price, target1))

    regime = _payload(snapshots, "regime") or {}
    state = str(regime.get("risk_state") or "")
    macro = levels_snap.get("macro") or {}
    if state in ("buys_halted", "all_halted") or macro.get("MACRO_RISK_OFF"):
        reasons = list(macro.get("reasons") or ())
        if state in ("buys_halted", "all_halted") and not reasons:
            reasons.append(str((regime.get("drivers") or [state])[0])[:160])
        out.append(Alert(BRAKE, "", None, None, "; ".join(reasons)))
    return out


class Ledger:
    """What was already sent today, on the bus (atomic across processes)."""

    def __init__(self, bus: Any) -> None:
        self.bus = bus

    def claim(self, alerts: list[Alert], today: date) -> list[Alert]:
        """The alerts not sent yet today, marked as sent in the same step."""
        fresh: list[Alert] = []
        day = today.isoformat()

        def mark(stored: Any) -> dict[str, Any]:
            sent = stored if isinstance(stored, dict) and stored.get("day") == day \
                else {"day": day, "keys": []}
            keys = set(sent.get("keys") or ())
            for alert in alerts:
                if alert.key not in keys:
                    keys.add(alert.key)
                    fresh.append(alert)
            return {"day": day, "keys": sorted(keys)}

        self.bus.update(KEY, mark)
        return fresh


def message(alert: Alert, lang: str = "ar") -> tuple[str, str]:
    """(title, body) for a desktop notification."""
    from .i18n import tr

    name = alert.ticker
    if alert.kind == STOP:
        return (tr("alert.stop.title", lang=lang, ticker=name),
                tr("alert.stop.body", lang=lang, price=f"{alert.price:,.2f}",
                   level=f"{alert.level:,.2f}"))
    if alert.kind == TARGET1:
        return (tr("alert.target1.title", lang=lang, ticker=name),
                tr("alert.target1.body", lang=lang, price=f"{alert.price:,.2f}",
                   level=f"{alert.level:,.2f}"))
    return (tr("alert.brake.title", lang=lang),
            tr("alert.brake.body", lang=lang, reason=alert.detail or "-"))


__all__ = ["Alert", "check", "Ledger", "message", "STOP", "TARGET1", "BRAKE"]
