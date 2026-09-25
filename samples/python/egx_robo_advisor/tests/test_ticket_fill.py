"""Filling a Thndr X buy ticket: the checks before, the script, and the read-back."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from egx_advisor.execution import ticket_fill as tf

NOW = datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)
TICKET = tf.TicketMap({
    "quantity": tf.FieldSpec(('input[name="qty"]',)),
    "price": tf.FieldSpec(('input[name="price"]',)),
})
BUY = tf.TicketOrder("COMI.CA", "buy", Decimal("100"), Decimal("85.50"), "under target")


def check(order=BUY, **overrides):
    kwargs = dict(plan_ts=NOW - timedelta(minutes=2), now=NOW, halted=False,
                  switched_on=True, ticket=TICKET)
    kwargs.update(overrides)
    return tf.check_fill(order, **kwargs)


def test_a_fresh_buy_with_taught_boxes_may_be_filled():
    assert check() is None


@pytest.mark.parametrize(("overrides", "key"), [
    ({"switched_on": False}, "ticket.off"),
    ({"halted": True}, "ticket.halted"),
    ({"plan_ts": None}, "ticket.no_plan"),
    ({"plan_ts": NOW - timedelta(minutes=16)}, "ticket.stale"),
    ({"plan_ts": NOW + timedelta(minutes=5)}, "ticket.stale"),
    ({"ticket": tf.TicketMap()}, "ticket.teach_first"),
    ({"ticket": tf.TicketMap({"quantity": TICKET.fields["quantity"]})}, "ticket.teach_first"),
])
def test_each_refusal(overrides, key):
    assert check(**overrides)[0] == key


@pytest.mark.parametrize(("order", "key"), [
    (tf.TicketOrder("COMI.CA", "sell", Decimal("100"), Decimal("85.5")), "ticket.buys_only"),
    (tf.TicketOrder("COMI.CA", "buy", Decimal("0"), Decimal("85.5")), "ticket.bad_quantity"),
    (tf.TicketOrder("COMI.CA", "buy", Decimal("10.5"), Decimal("85.5")), "ticket.bad_quantity"),
    (tf.TicketOrder("COMI.CA", "buy", Decimal("10"), Decimal("0")), "ticket.bad_price"),
])
def test_only_whole_positive_buys(order, key):
    assert check(order)[0] == key


def test_halt_is_checked_before_anything_else_that_could_pass():
    assert check(halted=True, ticket=tf.TicketMap())[0] == "ticket.halted"


def test_orders_come_from_the_plan_snapshot_and_bad_ones_are_skipped():
    payload = {"orders": [
        {"symbol": "COMI.CA", "side": "BUY", "quantity": "100", "limit_price": "85.50"},
        {"symbol": "ETEL.CA", "side": "sell", "quantity": "5", "limit_price": "30"},
        {"symbol": "BAD.CA", "side": "buy", "quantity": "lots", "limit_price": "1"},
        {"side": "buy"},
    ]}
    orders = tf.orders_from_plan(payload)
    assert [(o.symbol, o.side) for o in orders] == [("COMI.CA", "buy"), ("ETEL.CA", "sell")]
    assert orders[0].ticker == "COMI" and orders[0].price_text == "85.5"
    assert tf.orders_from_plan(None) == []


def test_numbers_as_a_ticket_shows_them():
    assert tf.parse_number("1,250") == Decimal("1250")
    assert tf.parse_number("٨٥٫٥٠") == Decimal("85.50")
    assert tf.parse_number(" 100 ") == Decimal("100")
    assert tf.parse_number("") is None and tf.parse_number("abc") is None


def test_read_back_must_match_the_order():
    assert tf.verify(BUY, {"ok": True, "quantity": "100", "price": "85.50"}) is None
    assert tf.verify(BUY, {"ok": True, "quantity": "1,00", "price": "85.5"}) is None
    assert tf.verify(BUY, {"ok": True, "quantity": "10", "price": "85.5"})[0] == "ticket.mismatch"
    assert tf.verify(BUY, {"ok": False, "reason": "field_missing"})[0] == "ticket.readback_failed"


def test_taught_boxes_survive_a_round_trip(tmp_path):
    path = tmp_path / "thndr.ticket.toml"
    tricky = tf.FieldSpec(('input[aria-label="Qty \\"shares\\""]', "body > div:nth-of-type(2)"),
                          placeholder="الكمية", label='say "hi"')
    ticket = tf.TicketMap().with_field("quantity", tricky).with_field("price", tricky)
    tf.save_ticket_map(path, ticket)
    assert tf.load_ticket_map(path) == ticket
    assert tf.load_ticket_map(tmp_path / "absent.toml") == tf.TicketMap()
    with pytest.raises(ValueError):
        tf.TicketMap().with_field("submit_button", tricky)


def test_a_picked_box_needs_a_way_to_find_it_again():
    spec = tf.field_from_picked({"ok": True, "candidates": ["#qty", ""], "placeholder": "Qty"})
    assert spec.candidates == ("#qty",)
    with pytest.raises(ValueError):
        tf.field_from_picked({"ok": True, "candidates": []})


FORBIDDEN = re.compile(r"\.click\(|submit|KeyboardEvent|keydown|keypress|keyup|'Enter'|\"Enter\"",
                       re.IGNORECASE)


def test_the_fill_and_read_back_scripts_only_write_two_values():
    for script in (tf.fill_script(BUY, TICKET), tf.readback_script(TICKET)):
        assert not FORBIDDEN.search(script), FORBIDDEN.search(script)
    assert not FORBIDDEN.search(tf.PICKER_SCRIPT.replace("'click', onClick", ""))
    assert tf.fill_script(BUY, TICKET).count("setValue.call(") == 1  # in the two-box loop


def test_order_values_cannot_break_out_of_the_script():
    order = tf.TicketOrder('X"</script><script>alert(1)//', "buy", Decimal("1"), Decimal("1"))
    script = tf.fill_script(order, TICKET)
    assert "</script>" not in script
    json.loads(script.split("ticker: ", 1)[1].split(",", 1)[0])  # still one JSON string


# --------------------------------------------------------------------------- #
# In a real browser, against a stand-in ticket page. Needs Playwright and a
# Chromium: set EGX_E2E_CHROMIUM to its executable to run these.
# --------------------------------------------------------------------------- #

FAKE_TICKET = """<!doctype html><html><body>
<h1>COMI - Commercial International Bank</h1>
<form id="ticket" onsubmit="window.log.push('submit'); return false;">
  <label>Quantity <input name="qty" type="text"></label>
  <label>Limit price <input name="price" inputmode="decimal"></label>
  <button id="buy" type="submit">Buy</button>
</form>
<script>
  window.log = [];
  window.state = {};
  for (const el of document.querySelectorAll('input')) {
    el.addEventListener('input', (e) => { window.state[e.target.name] = e.target.value; });
    el.addEventListener('mousedown', () => window.log.push('mousedown:' + el.name));
  }
  document.getElementById('buy').addEventListener('click', () => window.log.push('buy-click'));
  document.addEventListener('keydown', (e) => window.log.push('key:' + e.key));
</script></body></html>"""

CHROMIUM = os.environ.get("EGX_E2E_CHROMIUM", "")


@pytest.fixture
def page():
    if not CHROMIUM:
        pytest.skip("set EGX_E2E_CHROMIUM to a Chromium executable to run browser tests")
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM, args=["--no-sandbox"])
        tab = browser.new_page()
        tab.set_content(FAKE_TICKET)
        yield tab
        browser.close()


def run(tab, script):
    return json.loads(tab.evaluate(script))


def test_teaching_selects_the_box_without_thndr_seeing_the_click(page):
    run(page, tf.PICKER_SCRIPT)
    page.click("#buy")
    assert run(page, tf.PICKED_SCRIPT) == {"ok": False, "reason": "not_a_text_box"}
    page.click('input[name="qty"]')
    picked = run(page, tf.PICKED_SCRIPT)
    assert picked["ok"] and 'input[name="qty"]' in picked["candidates"]
    assert page.evaluate("window.log") == [], "the page saw a click meant for teaching"
    assert run(page, tf.PICKED_SCRIPT) is None  # read once, then cleared
    page.click("#buy")  # teaching is over: the page gets clicks again
    assert "buy-click" in page.evaluate("window.log")


def test_fill_writes_both_boxes_and_nothing_else(page):
    assert run(page, tf.fill_script(BUY, TICKET)) == {"ok": True}
    assert page.evaluate("window.state") == {"qty": "100", "price": "85.5"}
    assert page.evaluate("window.log") == [], "fill must not click, submit or press keys"
    readback = run(page, tf.readback_script(TICKET))
    assert tf.verify(BUY, readback) is None


def test_fill_refuses_on_the_wrong_stock_or_an_ambiguous_box(page):
    other = tf.TicketOrder("ETEL.CA", "buy", Decimal("5"), Decimal("30"))
    assert run(page, tf.fill_script(other, TICKET))["reason"] == "symbol_not_on_page"
    loose = tf.TicketMap({"quantity": tf.FieldSpec(("input",)),
                          "price": tf.FieldSpec(('input[name="price"]',))})
    assert run(page, tf.fill_script(BUY, loose)) == {
        "ok": False, "reason": "field_missing", "field": "quantity"}
    button = tf.TicketMap({"quantity": tf.FieldSpec(("#buy",)),
                           "price": tf.FieldSpec(('input[name="price"]',))})
    assert run(page, tf.fill_script(BUY, button))["reason"] == "not_a_text_box"
    same = tf.TicketMap({"quantity": TICKET.fields["price"], "price": TICKET.fields["price"]})
    assert run(page, tf.fill_script(BUY, same))["reason"] == "same_box"
    assert page.evaluate("window.state") == {} and page.evaluate("window.log") == []
