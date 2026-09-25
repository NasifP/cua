"""The panel beside the Thndr X browser that fills a buy ticket on request.

The logic and its safety checks are in execution/ticket_fill.py. This file is
the form: teach the two boxes, pick an order from the plan, press Fill, read
what happened. Every fill and refusal also goes to the bot's log.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWidgets import (
    QGridLayout,
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
PICK_POLL_MS = 300
READBACK_DELAY_MS = 400
#: Teaching gives up after this long: a page that navigated away has lost the picker.
PICK_TIMEOUT_MS = 120_000


class TicketPanel(QWidget):
    def __init__(self, page: Callable[[], QWebEnginePage],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._page = page
        self._ticket = self._load_ticket()
        self._orders: list[tf.TicketOrder] = []
        self._plan_ts: Optional[datetime] = None
        self._plan_key: Any = None
        self._halted = True
        self._picking: Optional[str] = None
        self._message: tuple[str, str, dict] = ("", "info", {})
        self._pick_timer = QTimer(self)
        self._pick_timer.timeout.connect(self._poll_picker)
        self._pick_deadline = QTimer(self)
        self._pick_deadline.setSingleShot(True)
        self._pick_deadline.timeout.connect(self.cancel_teaching)

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

        self.teach_box = QGroupBox()
        grid = QGridLayout(self.teach_box)
        self._field_labels: dict[str, QLabel] = {}
        self._field_states: dict[str, QLabel] = {}
        self._teach_buttons: dict[str, QPushButton] = {}
        for row, name in enumerate(tf.FIELDS):
            label, status, button = QLabel(), QLabel(), QPushButton()
            status.setAlignment(Qt.AlignCenter)
            button.clicked.connect(lambda _=False, n=name: self.teach(n))
            grid.addWidget(label, row, 0)
            grid.addWidget(status, row, 1)
            grid.addWidget(button, row, 2)
            self._field_labels[name] = label
            self._field_states[name] = status
            self._teach_buttons[name] = button
        self.cancel_button = QPushButton()
        self.cancel_button.setProperty("variant", "ghost")
        self.cancel_button.clicked.connect(self.cancel_teaching)
        self.cancel_button.hide()
        grid.addWidget(self.cancel_button, len(tf.FIELDS), 0, 1, 3)

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
        layout.addWidget(self.teach_box)
        layout.addWidget(self.orders_box, 1)
        layout.addWidget(self.message)
        self.retranslate()

    # ------------------------------------------------------------------ state

    @staticmethod
    def _load_ticket() -> tf.TicketMap:
        try:
            return tf.load_ticket_map(TICKET_FILE)
        except Exception:  # noqa: BLE001 - an unreadable file means "teach again"
            return tf.TicketMap()

    def retranslate(self) -> None:
        self.title.setText(tr("ticket.title"))
        self.intro.setText(tr("ticket.intro"))
        self.teach_box.setTitle(tr("ticket.teach_box"))
        for name in tf.FIELDS:
            self._field_labels[name].setText(tr(f"ticket.field.{name}"))
            self._teach_buttons[name].setText(tr("ticket.teach"))
        self.cancel_button.setText(tr("ticket.cancel"))
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
        except Exception:  # noqa: BLE001 - no bus: nothing may be filled
            halted, plan = True, {}
        key = (plan.get("ts"), halted, tf.enabled(os.environ))
        if key == self._plan_key:
            return
        self._plan_key = key
        self._halted = halted
        self._plan_ts = tf.parse_ts(plan.get("ts"))
        self._orders = [o for o in tf.orders_from_plan(plan.get("payload")) if o.side == "buy"]
        self._paint_orders()
        self._paint_state()

    def _paint_state(self) -> None:
        if not tf.enabled(os.environ):
            theme.say(self.state, tr("ticket.off"), "info")
        elif self._halted:
            theme.say(self.state, tr("ticket.halted"), "info")
        else:
            theme.say(self.state, "", "info")

    def _paint_fields(self) -> None:
        for name in tf.FIELDS:
            taught = name in self._ticket.fields
            status = self._field_states[name]
            status.setText(tr("ticket.taught") if taught else tr("ticket.not_taught"))
            theme.restyle_pill(status, "ok" if taught else "muted")
            self._teach_buttons[name].setEnabled(self._picking is None)
        self.cancel_button.setVisible(self._picking is not None)

    def _paint_orders(self) -> None:
        selected = self.orders.currentRow()
        self.orders.clear()
        for order in self._orders:
            item = QListWidgetItem(tr("ticket.order_line", symbol=order.symbol,
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

    # --------------------------------------------------------------- teaching

    def _run(self, script: str, callback: Callable[[Any], None]) -> None:
        # The app's own world: the page's scripts cannot see or change these.
        self._page().runJavaScript(script, QWebEngineScript.ApplicationWorld, callback)

    @Slot()
    def teach(self, name: str) -> None:
        self._picking = name
        self._paint_fields()
        self._say("ticket.picking", "info", field=tr(f"ticket.field.{name}"))
        self._pick_deadline.start(PICK_TIMEOUT_MS)
        self._run(tf.PICKER_SCRIPT, lambda _result: self._pick_timer.start(PICK_POLL_MS))

    @Slot()
    def cancel_teaching(self) -> None:
        self._pick_timer.stop()
        self._pick_deadline.stop()
        self._picking = None
        self._run(tf.CANCEL_PICKER_SCRIPT, lambda _result: None)
        self._paint_fields()
        self._say("")

    def _poll_picker(self) -> None:
        self._run(tf.PICKED_SCRIPT, self._picked)

    def _picked(self, result: Any) -> None:
        picked = tf.parse_result(result)
        name = self._picking
        if not picked or name is None:
            return
        if not picked.get("ok"):
            self._say("ticket.not_a_box", "bad")
            return
        try:
            self._ticket = self._ticket.with_field(name, tf.field_from_picked(picked))
            tf.save_ticket_map(TICKET_FILE, self._ticket)
        except (ValueError, OSError) as exc:
            self._say("ticket.page_error", "bad")
            self.message.setToolTip(str(exc))
        else:
            self._say("ticket.picked", "ok", field=tr(f"ticket.field.{name}"))
        self._pick_timer.stop()
        self._pick_deadline.stop()
        self._picking = None
        self._paint_fields()

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
            order, plan_ts=self._plan_ts, now=datetime.now(timezone.utc), halted=self._halted,
            switched_on=tf.enabled(os.environ), ticket=self._ticket,
        )
        if refusal is not None:
            key, values = refusal
            self._say(key, "bad", **values)
            self._log("guard", f"ticket not filled for {order.symbol}: {key}")
            return
        self.fill_button.setEnabled(False)
        self._run(tf.fill_script(order, self._ticket), lambda result: self._filled(order, result))

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
