"""The panel beside the Thndr X browser that fills a buy ticket on request.

The logic and its safety checks are in execution/ticket_fill.py. This file is
the form: pick an order from the plan, press Fill, read what happened. The two
boxes are taught in the Training tab (teach_box.py). Every fill and refusal
also goes to the bot's log.

A buy the chat analyst prepared (prepare_buy, "ticket_proposal" on the bus)
is listed first. For AUTOFILL_SECONDS after it was prepared the panel also
fills it on its own as soon as the operator opens that stock's buy ticket,
under the same check_fill conditions as a press on Fill. It still only writes
the two numbers; the operator checks them and presses Buy.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWidgets import (
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..execution import ticket_fill as tf
from ..i18n import tr
from ..paths import bus_path, config_path
from . import theme

TICKET_FILE = config_path("thndr.ticket.toml")
READBACK_DELAY_MS = 400


class TicketPanel(QWidget):
    def __init__(self, page: Callable[[], QWebEnginePage],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._page = page
        self._ticket = self._load_ticket()
        self._orders: list[tf.TicketOrder] = []
        #: When each listed order was made: the plan's time, or the proposal's.
        self._order_ts: dict[tf.TicketOrder, Optional[datetime]] = {}
        self._plan_ts: Optional[datetime] = None
        self._proposal: Optional[tf.TicketOrder] = None
        self._autofill_until: Optional[datetime] = None
        self._autofill_key: Any = None
        self._autofill_busy = False
        self._plan_key: Any = None
        self._halted = True
        #: True while the Training tab is teaching a box: no fill meanwhile.
        self.is_teaching: Callable[[], bool] = lambda: False
        self._message: tuple[str, str, dict] = ("", "info", {})

        self.setMinimumWidth(300)
        self.setMaximumWidth(420)
        self.title = QLabel()
        self.title.setObjectName("PageTitle")
        self.title.setStyleSheet("font-size: 13pt;")
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.intro.setProperty("muted", "true")
        self.state = QLabel()
        self.state.setWordWrap(True)

        self.teach_state = QLabel()
        self.teach_state.setWordWrap(True)
        self.teach_state.setProperty("muted", "true")

        self.orders_box = QGroupBox()
        orders_layout = QVBoxLayout(self.orders_box)
        self.steps = QLabel()
        self.steps.setWordWrap(True)
        self.steps.setProperty("muted", "true")
        self.orders = QListWidget()
        self.orders.currentRowChanged.connect(lambda _row: self._paint_fill_button())
        self.fill_button = QPushButton()
        self.fill_button.setProperty("variant", "primary")
        self.fill_button.clicked.connect(self.fill)
        orders_layout.addWidget(self.steps)
        orders_layout.addWidget(self.orders, 1)
        orders_layout.addWidget(self.fill_button)

        self.message = QLabel("")
        self.message.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(6)
        layout.addWidget(self.title)
        layout.addWidget(self.intro)
        layout.addWidget(self.state)
        layout.addWidget(self.teach_state)
        layout.addWidget(self.orders_box, 1)
        layout.addWidget(self.message)
        # Takes the free space only when the orders box is hidden (switched off).
        layout.addStretch(0)
        self.retranslate()

    # ------------------------------------------------------------------ state

    @staticmethod
    def _load_ticket() -> tf.TicketMap:
        from .teach_box import load_ticket

        return load_ticket(TICKET_FILE)

    def reload_ticket(self) -> None:
        """The boxes were taught again (Training tab)."""
        self._ticket = self._load_ticket()
        self._paint_fields()

    def retranslate(self) -> None:
        self.title.setText(tr("ticket.title"))
        self.intro.setText(tr("ticket.intro"))
        self.orders_box.setTitle(tr("ticket.orders_box"))
        self.steps.setText(tr("ticket.steps"))
        self.fill_button.setText(tr("ticket.fill"))
        self._paint_fields()
        self._paint_orders()
        self._paint_state()
        key, kind, values = self._message
        self._say(key, kind, **values)

    def refresh(self) -> None:
        """Re-read the plan and the halt state. The main window calls this every second."""
        try:
            from ..bus import StateBus

            with StateBus(bus_path()) as bus:
                halted = bus.control_state().halted
                plan = bus.get("plan") or {}
                proposal = (bus.get("ticket_proposal") or {}).get("payload") or {}
        except Exception:  # noqa: BLE001 - no bus: nothing may be filled
            halted, plan, proposal = True, {}, {}
        key = (plan.get("ts"), proposal.get("ts"), halted, tf.enabled(os.environ))
        if key != self._plan_key:
            self._plan_key = key
            self._halted = halted
            self._plan_ts = tf.parse_ts(plan.get("ts"))
            planned = [o for o in tf.orders_from_plan(plan.get("payload")) if o.side == "buy"]
            self._order_ts = {o: self._plan_ts for o in planned}
            self._proposal, self._autofill_until = None, None
            proposed = tf.orders_from_plan({"orders": [proposal]}) if proposal else []
            made = tf.parse_ts(proposal.get("ts"))
            if proposed and proposed[0].side == "buy" and made is not None \
                    and datetime.now(timezone.utc) - made <= tf.MAX_PLAN_AGE:
                self._proposal = proposed[0]
                self._autofill_until = tf.parse_ts(proposal.get("autofill_until"))
                self._order_ts[self._proposal] = made
            self._orders = ([self._proposal] if self._proposal else []) + \
                [o for o in planned if o != self._proposal]
            self._paint_orders()
            self._paint_state()
        self._autofill()

    def _paint_state(self) -> None:
        # Switched off (the default): only the title and one line saying how to
        # switch it on, instead of a column of disabled boxes beside Thndr X.
        on = tf.enabled(os.environ)
        for widget in (self.intro, self.teach_state, self.orders_box):
            widget.setVisible(on)
        self.setMinimumWidth(300 if on else 200)
        self.setMaximumWidth(420 if on else 240)
        if not on:
            theme.say(self.state, tr("ticket.off"), "info")
        elif self._halted:
            theme.say(self.state, tr("ticket.halted"), "info")
        else:
            theme.say(self.state, "", "info")

    def _paint_fields(self) -> None:
        taught = self._ticket.complete
        self.teach_state.setText(tr("ticket.taught_all") if taught else tr("ticket.teach_where"))
        self.teach_state.setProperty("muted", "true" if taught else "false")
        theme.restyle(self.teach_state)

    def _paint_orders(self) -> None:
        selected = self.orders.currentRow()
        self.orders.clear()
        for order in self._orders:
            line = "ticket.proposal_line" if order == self._proposal else "ticket.order_line"
            item = QListWidgetItem(tr(line, symbol=order.symbol,
                                      quantity=order.quantity_text, price=order.price_text))
            item.setToolTip(order.rationale)
            self.orders.addItem(item)
        if not self._orders:
            placeholder = QListWidgetItem(tr("ticket.no_orders"))
            placeholder.setFlags(Qt.NoItemFlags)
            self.orders.addItem(placeholder)
        elif 0 <= selected < len(self._orders):
            self.orders.setCurrentRow(selected)
        else:
            self.orders.setCurrentRow(0)
        self._paint_fill_button()

    def _paint_fill_button(self) -> None:
        row = self.orders.currentRow()
        self.fill_button.setEnabled(bool(self._orders) and 0 <= row < len(self._orders))

    def _say(self, key: str, kind: str = "info", **values: Any) -> None:
        self._message = (key, kind, values)
        theme.say(self.message, tr(key, **values) if key else "", kind)

    def _run(self, script: str, callback: Callable[[Any], None]) -> None:
        # The app's own world: the page's scripts cannot see or change these.
        self._page().runJavaScript(script, QWebEngineScript.ApplicationWorld, callback)

    # ------------------------------------------------------------------ filling

    @Slot()
    def fill(self) -> None:
        row = self.orders.currentRow()
        if not 0 <= row < len(self._orders):
            return
        order = self._orders[row]  # the order the operator is looking at
        self._plan_key = None
        self.refresh()  # the halt state and the plan as of now, not as of the last tick
        if order not in self._orders:
            # A new plan arrived between choosing and pressing: never fill a
            # different order from the one on screen.
            self._say("ticket.plan_changed", "bad")
            return
        refusal = tf.check_fill(
            order, plan_ts=self._order_ts.get(order), now=datetime.now(timezone.utc),
            halted=self._halted, switched_on=tf.enabled(os.environ), ticket=self._ticket,
        )
        if refusal is not None:
            key, values = refusal
            self._say(key, "bad", **values)
            self._log("guard", f"ticket not filled for {order.symbol}: {key}")
            return
        self.fill_button.setEnabled(False)
        self._run(tf.fill_script(order, self._ticket), lambda result: self._filled(order, result))

    # ------------------------------------------------------- the analyst's buy

    def _autofill(self) -> None:
        """Fill the analyst's prepared buy once its ticket is open. Every tick."""
        order, until = self._proposal, self._autofill_until
        if order is None or until is None or self._autofill_busy:
            return
        done_key = (order, until)
        if self._autofill_key == done_key:
            return
        now = datetime.now(timezone.utc)
        if now > until or self.is_teaching():
            return
        refusal = tf.check_fill(order, plan_ts=self._order_ts.get(order), now=now,
                                halted=self._halted, switched_on=tf.enabled(os.environ),
                                ticket=self._ticket)
        if refusal is not None:
            self._autofill_key = done_key  # say it once, not every second
            key, values = refusal
            self._say(key, "bad", **values)
            return
        self._autofill_busy = True

        def result(value: Any) -> None:
            self._autofill_busy = False
            outcome = tf.parse_result(value) or {}
            # The ticket is not open yet: keep waiting for the operator.
            if outcome.get("reason") in ("field_missing", "symbol_not_on_page"):
                if not self._message[0]:
                    self._say("ticket.waiting", "info", ticker=order.ticker)
                return
            self._autofill_key = done_key
            self._filled(order, value)

        self._run(tf.fill_script(order, self._ticket), result)

    def _filled(self, order: tf.TicketOrder, result: Any) -> None:
        outcome = tf.parse_result(result)
        if not outcome or not outcome.get("ok"):
            reason = (outcome or {}).get("reason", "page_error")
            field = tr(f"ticket.field.{(outcome or {}).get('field', 'quantity')}")
            self._say(f"ticket.{reason}", "bad", ticker=order.ticker, field=field)
            self._log("guard", f"ticket not filled for {order.symbol}: {reason}")
            self._paint_fill_button()
            return
        QTimer.singleShot(READBACK_DELAY_MS, lambda: self._run(
            tf.readback_script(self._ticket), lambda value: self._checked(order, value)))

    def _checked(self, order: tf.TicketOrder, result: Any) -> None:
        self._paint_fill_button()
        readback = tf.parse_result(result) or {"ok": False, "reason": "page_error"}
        problem = tf.verify(order, readback)
        if problem is not None:
            key, values = problem
            self._say(key, "bad", **values)
            self._log("guard", f"ticket for {order.symbol} does not match after filling: "
                               f"{readback}")
            return
        self._say("ticket.filled", "ok", symbol=order.symbol, quantity=order.quantity_text,
                  price=order.price_text)
        self._log("order", f"ticket filled in the app, NOT submitted: BUY {order.quantity_text} "
                           f"{order.symbol} @ {order.price_text}; the operator presses Buy")

    @staticmethod
    def _log(kind: str, message: str) -> None:
        try:
            from ..bus import EventKind, StateBus

            with StateBus(bus_path()) as bus:
                bus.publish(EventKind(kind), message, phase="ticket")
        except Exception:  # noqa: BLE001 - the log is a record, never a gate
            pass
