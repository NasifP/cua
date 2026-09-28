"""Teaching the two Thndr X ticket boxes (quantity and price), in the Training tab.

The operator opens any buy ticket in the Thndr X window, presses Teach here
and clicks the box there. `ticket_fill.PICKER_SCRIPT` makes that click select
the box instead of reaching Thndr X. The result goes to config/thndr.ticket.toml,
which the ticket panel beside Thndr X fills from.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QPushButton, QWidget

from ..execution import ticket_fill as tf
from ..i18n import tr
from ..paths import config_path
from . import theme

TICKET_FILE = config_path("thndr.ticket.toml")
PICK_POLL_MS = 300
#: Teaching gives up after this long: a page that navigated away has lost the picker.
PICK_TIMEOUT_MS = 120_000


def load_ticket(path=None) -> tf.TicketMap:
    try:
        return tf.load_ticket_map(path or TICKET_FILE)
    except Exception:  # noqa: BLE001 - an unreadable file means "teach again"
        return tf.TicketMap()


class TeachBox(QGroupBox):
    def __init__(self, page: Callable[[], QWebEnginePage],
                 on_taught: Callable[[], None] = lambda: None,
                 on_start: Callable[[], None] = lambda: None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._page = page
        self._on_taught = on_taught
        #: Called before the picker starts, so the Thndr X window can come forward.
        self._on_start = on_start
        self.ticket = load_ticket(TICKET_FILE)
        self.picking: Optional[str] = None
        self._message: tuple[str, str, dict] = ("", "info", {})
        self._pick_timer = QTimer(self)
        self._pick_timer.timeout.connect(self._poll_picker)
        self._pick_deadline = QTimer(self)
        self._pick_deadline.setSingleShot(True)
        self._pick_deadline.timeout.connect(self.cancel_teaching)

        grid = QGridLayout(self)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        self.intro.setProperty("muted", "true")
        grid.addWidget(self.intro, 0, 0, 1, 3)
        self._field_labels: dict[str, QLabel] = {}
        self._field_states: dict[str, QLabel] = {}
        self._teach_buttons: dict[str, QPushButton] = {}
        for row, name in enumerate(tf.FIELDS, start=1):
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
        grid.addWidget(self.cancel_button, len(tf.FIELDS) + 1, 0, 1, 3)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        grid.addWidget(self.message, len(tf.FIELDS) + 2, 0, 1, 3)
        self.retranslate()

    def retranslate(self) -> None:
        self.setTitle(tr("ticket.teach_box"))
        self.intro.setText(tr("train.ticket_intro"))
        for name in tf.FIELDS:
            self._field_labels[name].setText(tr(f"ticket.field.{name}"))
            self._teach_buttons[name].setText(tr("ticket.teach"))
        self.cancel_button.setText(tr("ticket.cancel"))
        self._paint_fields()
        key, kind, values = self._message
        self._say(key, kind, **values)

    def _paint_fields(self) -> None:
        for name in tf.FIELDS:
            taught = name in self.ticket.fields
            status = self._field_states[name]
            status.setText(tr("ticket.taught") if taught else tr("ticket.not_taught"))
            theme.restyle_pill(status, "ok" if taught else "muted")
            self._teach_buttons[name].setEnabled(self.picking is None)
        self.cancel_button.setVisible(self.picking is not None)

    def _say(self, key: str, kind: str = "info", **values: Any) -> None:
        self._message = (key, kind, values)
        theme.say(self.message, tr(key, **values) if key else "", kind)

    def _run(self, script: str, callback: Callable[[Any], None]) -> None:
        # The app's own world: the page's scripts cannot see or change these.
        self._page().runJavaScript(script, QWebEngineScript.ApplicationWorld, callback)

    @Slot()
    def teach(self, name: str) -> None:
        self._on_start()
        self.picking = name
        self._paint_fields()
        self._say("ticket.picking", "info", field=tr(f"ticket.field.{name}"))
        self._pick_deadline.start(PICK_TIMEOUT_MS)
        self._run(tf.PICKER_SCRIPT, lambda _result: self._pick_timer.start(PICK_POLL_MS))

    @Slot()
    def cancel_teaching(self) -> None:
        self._pick_timer.stop()
        self._pick_deadline.stop()
        self.picking = None
        self._run(tf.CANCEL_PICKER_SCRIPT, lambda _result: None)
        self._paint_fields()
        self._say("")

    def _poll_picker(self) -> None:
        self._run(tf.PICKED_SCRIPT, self._picked)

    def _picked(self, result: Any) -> None:
        picked = tf.parse_result(result)
        name = self.picking
        if not picked or name is None:
            return
        if not picked.get("ok"):
            self._say("ticket.not_a_box", "bad")
            return
        try:
            self.ticket = self.ticket.with_field(name, tf.field_from_picked(picked))
            tf.save_ticket_map(TICKET_FILE, self.ticket)
        except (ValueError, OSError) as exc:
            self._say("ticket.page_error", "bad")
            self.message.setToolTip(str(exc))
        else:
            self._say("ticket.picked", "ok", field=tr(f"ticket.field.{name}"))
            self._on_taught()
        self._pick_timer.stop()
        self._pick_deadline.stop()
        self.picking = None
        self._paint_fields()
