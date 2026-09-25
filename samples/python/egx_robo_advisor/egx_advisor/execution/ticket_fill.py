"""Fill a Thndr X buy ticket in the app's browser. You press Buy; this never does.

The agent stays read-only: it plans, and nothing it runs can type into the
page. This module is used by the desktop app, and only when the operator
presses Fill on one order from the plan. It then writes two numbers -- the
quantity and the limit price -- into the two input boxes the operator taught
it, and reads them back. It sends no click, no key and no Enter, and it
never looks for the Buy button.

Before it writes anything, `check_fill` must pass:

- the feature is switched on in Settings (EGX_TICKET_FILL, off by default);
- the bot is not halted: HALT stops ticket filling too;
- the order is a buy from the current plan, with a positive quantity and price;
- that plan is at most MAX_PLAN_AGE old, so its limit price is recent;
- both fields have been taught.

The script itself then refuses unless the stock's ticker is on the page, each
field matches exactly one visible, enabled text input, and the two are
different boxes.

Teaching: `picker_script` makes the next click on the page select a box
instead of acting on it (the click never reaches Thndr X), and records a few
ways to find that box again (config/thndr.ticket.toml).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

FIELDS = ("quantity", "price")
MAX_PLAN_AGE = timedelta(minutes=15)


# --------------------------------------------------------------------------- #
# The taught fields: config/thndr.ticket.toml
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldSpec:
    #: CSS selectors, most specific first. A fill uses the first one that
    #: matches exactly one visible text input.
    candidates: tuple[str, ...]
    placeholder: str = ""
    label: str = ""


@dataclass(frozen=True)
class TicketMap:
    fields: Mapping[str, FieldSpec] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return all(self.fields.get(name) and self.fields[name].candidates for name in FIELDS)

    def with_field(self, name: str, spec: FieldSpec) -> "TicketMap":
        if name not in FIELDS:
            raise ValueError(f"unknown ticket field {name!r}")
        return TicketMap({**self.fields, name: spec})


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def load_ticket_map(path: Path) -> TicketMap:
    if not path.exists():
        return TicketMap()
    import tomllib

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    fields = {}
    for name in FIELDS:
        entry = raw.get(name)
        if isinstance(entry, dict) and entry.get("candidates"):
            fields[name] = FieldSpec(
                candidates=tuple(str(c) for c in entry["candidates"]),
                placeholder=str(entry.get("placeholder", "")),
                label=str(entry.get("label", "")),
            )
    return TicketMap(fields)


def save_ticket_map(path: Path, ticket: TicketMap) -> None:
    lines = [
        "# The Thndr X order-ticket boxes the desktop app fills, taught by clicking",
        "# them (Thndr X tab -> Teach). Only these two boxes are ever written to;",
        "# nothing is ever clicked. Delete this file to teach them again.",
        "",
    ]
    for name in FIELDS:
        spec = ticket.fields.get(name)
        if spec is None:
            continue
        lines += [
            f"[{name}]",
            "candidates = [" + ", ".join(_toml_string(c) for c in spec.candidates) + "]",
            f"placeholder = {_toml_string(spec.placeholder)}",
            f"label = {_toml_string(spec.label)}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def field_from_picked(picked: Mapping[str, Any]) -> FieldSpec:
    candidates = tuple(str(c) for c in picked.get("candidates") or () if str(c).strip())
    if not candidates:
        raise ValueError("the picked box gave no way to find it again")
    return FieldSpec(candidates[:8], str(picked.get("placeholder") or "")[:80],
                     str(picked.get("label") or "")[:80])


# --------------------------------------------------------------------------- #
# Orders and the checks before a fill
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TicketOrder:
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    rationale: str = ""

    @property
    def ticker(self) -> str:
        """COMI.CA -> COMI, the way Thndr X shows it."""
        return self.symbol.upper().removesuffix(".CA")

    @property
    def quantity_text(self) -> str:
        return format(self.quantity.normalize(), "f")

    @property
    def price_text(self) -> str:
        return format(self.limit_price.normalize(), "f")


def orders_from_plan(payload: Optional[Mapping[str, Any]]) -> list[TicketOrder]:
    """Every order in the plan snapshot, in its order. Malformed ones are skipped."""
    orders = []
    for raw in (payload or {}).get("orders") or ():
        try:
            orders.append(TicketOrder(
                symbol=str(raw["symbol"]), side=str(raw["side"]).lower(),
                quantity=Decimal(str(raw["quantity"])),
                limit_price=Decimal(str(raw["limit_price"])),
                rationale=str(raw.get("rationale") or ""),
            ))
        except (KeyError, InvalidOperation, TypeError):
            continue
    return orders


def enabled(env: Mapping[str, str]) -> bool:
    return (env.get("EGX_TICKET_FILL") or "").strip().lower() == "true"


def check_fill(
    order: TicketOrder,
    *,
    plan_ts: Optional[datetime],
    now: datetime,
    halted: bool,
    switched_on: bool,
    ticket: TicketMap,
) -> Optional[tuple[str, dict[str, Any]]]:
    """Why this order must not be filled now, as an i18n key and its values; None if it may."""
    if not switched_on:
        return "ticket.off", {}
    if halted:
        return "ticket.halted", {}
    if order.side != "buy":
        return "ticket.buys_only", {}
    if order.quantity <= 0 or order.quantity != order.quantity.to_integral_value():
        return "ticket.bad_quantity", {"quantity": order.quantity}
    if order.limit_price <= 0:
        return "ticket.bad_price", {"price": order.limit_price}
    if plan_ts is None:
        return "ticket.no_plan", {}
    age = now - plan_ts
    if age > MAX_PLAN_AGE or age < -timedelta(minutes=1):
        return "ticket.stale", {"minutes": int(age.total_seconds() // 60),
                                "limit": int(MAX_PLAN_AGE.total_seconds() // 60)}
    if not ticket.complete:
        return "ticket.teach_first", {}
    return None


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")


def parse_number(text: Any) -> Optional[Decimal]:
    """A number as a Thndr X box shows it: Arabic digits, thousands commas, spaces."""
    cleaned = re.sub(r"[,\s٬ ]", "", str(text or "").translate(_ARABIC_DIGITS))
    try:
        return Decimal(cleaned) if cleaned else None
    except InvalidOperation:
        return None


def verify(order: TicketOrder, readback: Mapping[str, Any]) -> Optional[tuple[str, dict]]:
    """Compare what the boxes now hold with the order. None when both match."""
    if not readback.get("ok"):
        return "ticket.readback_failed", {"reason": readback.get("reason", "?")}
    quantity = parse_number(readback.get("quantity"))
    price = parse_number(readback.get("price"))
    if quantity != order.quantity or price != order.limit_price:
        return "ticket.mismatch", {"quantity": readback.get("quantity"),
                                   "price": readback.get("price")}
    return None


# --------------------------------------------------------------------------- #
# Page scripts. Each returns a JSON string. Run them in an isolated world.
# --------------------------------------------------------------------------- #


def _js(value: Any) -> str:
    # JSON is a JavaScript literal; escaping "<" keeps "</script>" inert too.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


_FIND = """
  function find(candidates) {
    for (const selector of candidates) {
      let nodes;
      try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
      const shown = [...nodes].filter((n) => n.getClientRects().length > 0);
      if (shown.length === 1) return shown[0];
    }
    return null;
  }
  const TEXT_TYPES = ['text', 'number', 'tel', 'search', 'decimal', ''];
  function textInput(el) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    return el instanceof HTMLInputElement && TEXT_TYPES.includes(type);
  }
"""


def fill_script(order: TicketOrder, ticket: TicketMap) -> str:
    """Write the order's quantity and limit price into the two taught boxes. Nothing else."""
    fields = {name: list(ticket.fields[name].candidates) for name in FIELDS}
    return f"""(function () {{
  const want = {{ ticker: {_js(order.ticker)}, quantity: {_js(order.quantity_text)},
                 price: {_js(order.price_text)} }};
  const fields = {_js(fields)};
{_FIND}
  const page = ((document.body && document.body.innerText) || '').toUpperCase();
  const ticker = want.ticker.replace(/[^A-Z0-9]/g, '');
  const word = new RegExp('(^|[^A-Z0-9])' + ticker + '([^A-Z0-9]|$)');
  if (!word.test(page)) return JSON.stringify({{ ok: false, reason: 'symbol_not_on_page' }});
  const refuse = (reason, field) => JSON.stringify({{ ok: false, reason, field }});
  const boxes = {{}};
  for (const name of ['quantity', 'price']) {{
    const el = find(fields[name]);
    if (!el) return refuse('field_missing', name);
    if (!textInput(el)) return refuse('not_a_text_box', name);
    if (el.disabled || el.readOnly) return refuse('field_locked', name);
    boxes[name] = el;
  }}
  if (boxes.quantity === boxes.price) return JSON.stringify({{ ok: false, reason: 'same_box' }});
  // The prototype's setter, so a React-controlled box registers the change.
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  for (const [name, value] of [['quantity', want.quantity], ['price', want.price]]) {{
    const el = boxes[name];
    el.focus();
    setValue.call(el, value);
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    el.blur();
  }}
  return JSON.stringify({{ ok: true }});
}})()"""


def readback_script(ticket: TicketMap) -> str:
    fields = {name: list(ticket.fields[name].candidates) for name in FIELDS}
    return f"""(function () {{
  const fields = {_js(fields)};
{_FIND}
  const out = {{ ok: true }};
  for (const name of ['quantity', 'price']) {{
    const el = find(fields[name]);
    if (!el) return JSON.stringify({{ ok: false, reason: 'field_missing', field: name }});
    out[name] = el.value;
  }}
  return JSON.stringify(out);
}})()"""


PICKER_SCRIPT = """(function () {
  if (window.__egxPicker) window.__egxPicker.stop();
  window.__egxPicked = null;
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;' +
    'border:2px solid #38bdf8;border-radius:6px;background:rgba(56,189,248,.15);display:none';
  const BLOCKED = ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'dblclick', 'touchstart',
                   'touchend', 'contextmenu'];
  function attr(el, name) {
    const v = el.getAttribute(name);
    if (!v) return null;
    const quoted = v.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
    return `${el.tagName.toLowerCase()}[${name}="${quoted}"]`;
  }
  function path(el) {
    const parts = [];
    let n = el;
    for (; n && n.nodeType === 1 && n !== document.body && parts.length < 12; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.tagName === n.tagName) i++;
      }
      parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    return 'body > ' + parts.join(' > ');
  }
  function describe(el) {
    const c = [];
    if (el.id && !/\\d{3,}/.test(el.id)) c.push('#' + CSS.escape(el.id));
    for (const name of ['data-testid', 'data-test', 'name', 'aria-label', 'placeholder']) {
      const s = attr(el, name);
      if (s) c.push(s);
    }
    c.push(path(el));
    const first = el.labels && el.labels[0];
    const label = (first && first.innerText) || el.getAttribute('aria-label') || '';
    const placeholder = el.getAttribute('placeholder') || '';
    return { ok: true, candidates: c, placeholder, label: label.trim() };
  }
  function target(e) { return e.target && e.target.closest ? e.target.closest('input') : null; }
  function block(e) { e.preventDefault(); e.stopImmediatePropagation(); }
  function onClick(e) {
    block(e);
    const el = target(e);
    const types = ['text', 'number', 'tel', 'search', 'decimal', ''];
    if (!el || !types.includes((el.getAttribute('type') || '').toLowerCase())) {
      window.__egxPicked = { ok: false, reason: 'not_a_text_box' };
      return;
    }
    window.__egxPicked = describe(el);
    stop();
  }
  function onMove(e) {
    const el = target(e);
    if (!el) { box.style.display = 'none'; return; }
    const r = el.getBoundingClientRect();
    Object.assign(box.style, { display: 'block', left: r.left - 3 + 'px', top: r.top - 3 + 'px',
                               width: r.width + 6 + 'px', height: r.height + 6 + 'px' });
  }
  function stop() {
    BLOCKED.forEach((t) => window.removeEventListener(t, block, true));
    window.removeEventListener('click', onClick, true);
    window.removeEventListener('mousemove', onMove, true);
    box.remove();
    window.__egxPicker = null;
  }
  // Capture on window runs before anything on the page, so the click that
  // picks a box never reaches Thndr X.
  BLOCKED.forEach((t) => window.addEventListener(t, block, true));
  window.addEventListener('click', onClick, true);
  window.addEventListener('mousemove', onMove, true);
  document.documentElement.appendChild(box);
  window.__egxPicker = { stop };
  return JSON.stringify({ ok: true });
})()"""

#: Returns and clears what the picker recorded ("null" while still waiting).
PICKED_SCRIPT = """(function () {
  const picked = window.__egxPicked || null;
  window.__egxPicked = null;
  return JSON.stringify(picked);
})()"""

CANCEL_PICKER_SCRIPT = """(function () {
  if (window.__egxPicker) window.__egxPicker.stop();
  window.__egxPicked = null;
  return 'null';
})()"""


def parse_result(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None
