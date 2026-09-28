"""Today's action cards: one per stock, what to do and why, from what the app knows.

Three sources, most specific first:

1. the analyst team's decision (analyst/committee.py, the lead's DECISION
   lines, kept on the bus as "team_views") when it is from today;
2. the bot's plan (its buy and sell orders, from the four-factor rules);
3. stops and targets (levels.py): a holding at or under its stop, or above
   its first target, gets a card even when nobody asked about it.

A card says the ticker, the decision (buy / add / hold / trim / sell), a short
reason, and whether "Prepare order" can be pressed. Preparing goes through
Toolbox.prepare_buy, the same checks as the analyst's (cash, price near the
market, news brake, HALT, liquidity), and only ever fills a ticket: the
operator presses Buy. There is no sell ticket in this app, so a sell or trim
card opens the stock in Thndr X instead.

Nothing here calls a model. Building the cards reads the bus and costs nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional

BUY_SIDE = ("buy", "add")
SELL_SIDE = ("trim", "sell")
#: Order of the cards: things to protect first, then buys, then holds.
_ORDER = {"sell": 0, "trim": 1, "buy": 2, "add": 3, "hold": 4}


@dataclass(frozen=True)
class Card:
    ticker: str
    decision: str
    reason: str
    #: "team" (the analyst team), "plan" (the bot's rules) or "levels" (stop/target).
    source: str
    quantity: Optional[int] = None
    limit_price: Optional[float] = None
    #: The risk manager's cap on this trade, EGP; None when there is none.
    max_egp: Optional[float] = None
    #: Whether "Prepare order" is offered, and why not when it is not.
    can_prepare: bool = False
    blocked: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.ticker + ".CA"

    @property
    def is_buy(self) -> bool:
        return self.decision in BUY_SIDE


def _payload(snapshots: Mapping[str, Any], key: str) -> Any:
    entry = snapshots.get(key)
    return (entry or {}).get("payload") if isinstance(entry, Mapping) else None


def _ticker(symbol: Any) -> str:
    from .browse import ticker_of

    return ticker_of(str(symbol or ""))


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build(snapshots: Mapping[str, Any], today: date, halted: bool = False) -> list[Card]:
    """The day's cards from the bus's snapshots (StateBus.all_snapshots())."""
    cards: dict[str, Card] = {}
    regime = _payload(snapshots, "regime") or {}
    brake = str(regime.get("risk_state") or "") in ("buys_halted", "all_halted")
    blocked_symbols = {_ticker(s) for s in regime.get("blocked_symbols") or ()}
    levels = (_payload(snapshots, "levels") or {}).get("levels") or {}
    positions = {_ticker(p.get("symbol")): p
                 for p in (_payload(snapshots, "portfolio") or {}).get("positions") or ()}

    def price_of(ticker: str) -> Optional[float]:
        lv = levels.get(ticker + ".CA") or {}
        if _num(lv.get("price")):
            return _num(lv.get("price"))
        p = positions.get(ticker) or {}
        qty, value = _num(p.get("quantity")), _num(p.get("market_value"))
        return round(value / qty, 3) if qty and value else None

    # 3. stops and targets
    for symbol, lv in levels.items():
        ticker = _ticker(symbol)
        status = lv.get("status")
        if not ticker or status not in ("below_stop", "near_stop", "above_target1",
                                        "above_target2"):
            continue
        decision = {"below_stop": "sell", "near_stop": "trim", "above_target1": "hold",
                    "above_target2": "trim"}[status]
        reason = {"below_stop": f"under its stop {lv.get('stop')}",
                  "near_stop": f"within one ATR of its stop {lv.get('stop')}",
                  "above_target1": f"past target 1 {lv.get('target1')}: raise the stop",
                  "above_target2": f"past target 2 {lv.get('target2')}: take some profit"}[status]
        cards[ticker] = Card(ticker, decision, reason, "levels", limit_price=_num(lv.get("price")),
                             details={"levels": lv})

    # 2. the bot's plan
    plan = _payload(snapshots, "plan") or {}
    for order in plan.get("orders") or ():
        ticker = _ticker(order.get("symbol"))
        if not ticker:
            continue
        side = "buy" if order.get("side") == "buy" else "sell"
        if side == "buy" and ticker in positions:
            side = "add"
        qty = _num(order.get("quantity"))
        cards[ticker] = Card(ticker, side, str(order.get("rationale") or "the bot's plan")[:140],
                             "plan", quantity=int(qty) if qty else None,
                             limit_price=_num(order.get("limit_price")),
                             details={"plan_as_of": plan.get("as_of")})

    # 1. the analyst team, today only
    for ticker, view in (_payload(snapshots, "team_views") or {}).items():
        ticker = _ticker(ticker)
        if not ticker or view.get("day") != today.isoformat():
            continue
        decision = str(view.get("decision") or "")
        if decision not in _ORDER:
            continue
        earlier = cards.get(ticker)
        cards[ticker] = Card(
            ticker, decision, str(view.get("reason") or "")[:140] or "the analyst team's view",
            "team", quantity=earlier.quantity if earlier and earlier.is_buy else None,
            limit_price=(earlier.limit_price if earlier else None) or price_of(ticker),
            max_egp=_num(view.get("max_egp")),
            details={"team_buy_allowed": bool(view.get("buy_allowed")),
                     "gate_reasons": list(view.get("gate_reasons") or ())})

    out = []
    for card in cards.values():
        if card.limit_price is None:
            card = _with(card, limit_price=price_of(card.ticker))
        out.append(_with(card, **_preparable(card, halted, brake, blocked_symbols)))
    return sorted(out, key=lambda c: (_ORDER.get(c.decision, 9), c.ticker))


def _preparable(card: Card, halted: bool, brake: bool, blocked: set[str]) -> dict[str, Any]:
    """can_prepare and blocked: a first answer for the button; prepare_buy decides."""
    if not card.is_buy:
        return {"can_prepare": False, "blocked": "sell_side"}
    if halted:
        return {"can_prepare": False, "blocked": "halted"}
    if brake or card.ticker in blocked:
        return {"can_prepare": False, "blocked": "news_brake"}
    if card.source == "team" and not card.details.get("team_buy_allowed"):
        return {"can_prepare": False, "blocked": "team_gate"}
    return {"can_prepare": True, "blocked": ""}


def _with(card: Card, **changes: Any) -> Card:
    from dataclasses import replace

    return replace(card, **changes)


def suggested_quantity(card: Card, cash_egp: Optional[float],
                       liquidity_max: Optional[int]) -> int:
    """Shares to pre-fill: the plan's count, else what the cap, cash and liquidity allow."""
    price = card.limit_price or 0
    if price <= 0:
        return 0
    limits = [card.quantity] if card.quantity else []
    if card.max_egp:
        limits.append(int(card.max_egp // price))
    if cash_egp is not None:
        limits.append(int(max(cash_egp, 0) // price))
    if liquidity_max is not None:
        limits.append(liquidity_max)
    return max(min(limits), 0) if limits else 0


__all__ = ["Card", "build", "suggested_quantity", "BUY_SIDE", "SELL_SIDE"]
