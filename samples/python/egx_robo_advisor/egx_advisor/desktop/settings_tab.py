"""The Settings tab: API keys, models, limits, and a read-only view of the rest.

Storage and validation live in egx_advisor/settings.py; this is only the form.
API key fields are write-only: they never show a stored key, only where it is
stored, and an empty field on save means "leave it as it is".
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import settings
from ..execution.thndr import ThndrUiMap
from ..paths import ui_map_path
from . import theme


class _TestSignals(QObject):
    finished = Signal(str, bool, str)


class SettingsTab(QWidget):
    """Form over settings.load()/save(). `on_saved` restarts the bot processes."""

    def __init__(self, on_saved: Callable[[], None], store: Optional[settings.SecretStore] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._on_saved = on_saved
        self._store = store or settings.SecretStore()
        self._secret_inputs: dict[str, QLineEdit] = {}
        self._secret_status: dict[str, QLabel] = {}
        self._remove: set[str] = set()
        self._inputs: dict[str, QWidget] = {}
        self._signals = _TestSignals()
        self._signals.finished.connect(self._show_test_result)

        body = QWidget()
        column = QVBoxLayout(body)
        column.addWidget(self._keys_box())
        column.addWidget(self._models_box())
        column.addWidget(self._behaviour_box())
        column.addWidget(self._fixed_box())
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)

        self.save_button = QPushButton("حفظ الإعدادات")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setMinimumWidth(160)
        self.save_button.clicked.connect(self.save)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.message, 1)
        footer.addWidget(self.save_button)

        column.setContentsMargins(24, 0, 24, 12)
        column.setSpacing(4)
        footer.setContentsMargins(24, 10, 24, 16)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addLayout(footer)
        self.reload()

    # ------------------------------------------------------------------ building

    def _keys_box(self) -> QGroupBox:
        box = QGroupBox("مفاتيح API")
        form = QFormLayout(box)
        if not self._store.available:
            where = ".env (no credential store on this system)"
        elif os.name == "nt":
            where = "Windows Credential Manager"
        else:
            where = "the system keyring"
        note = QLabel(f"المفاتيح بتتحفظ في {where}، ومش بتظهر تاني بعد الحفظ. "
                      "لو عاوز تغيّر مفتاح، اكتب الجديد مكانه.")
        note.setProperty("muted", "true")
        note.setWordWrap(True)
        form.addRow(note)
        for spec in settings.SECRET_FIELDS:
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            edit.setPlaceholderText(spec.help)
            status = QLabel("")
            status.setMinimumWidth(90)
            status.setAlignment(Qt.AlignCenter)
            remove = QPushButton("حذف")
            remove.setProperty("variant", "ghost")
            remove.setToolTip(f"Remove the stored {spec.key}")
            remove.clicked.connect(lambda _=False, k=spec.key: self._mark_remove(k))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(status)
            row.addWidget(remove)
            form.addRow(spec.label, row)
            self._secret_inputs[spec.key] = edit
            self._secret_status[spec.key] = status
        return box

    def _models_box(self) -> QGroupBox:
        box = QGroupBox("الموديلات")
        form = QFormLayout(box)
        note = QLabel(settings.MODEL_FIELDS[1].help)
        note.setWordWrap(True)
        form.addRow(note)
        for spec in settings.MODEL_FIELDS:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(list(spec.suggestions))
            combo.setToolTip(spec.help)
            if not spec.default:
                combo.lineEdit().setPlaceholderText("empty: same as the Chat model")
            test = QPushButton("جرّب")
            test.setToolTip("Send one short request with the saved key")
            test.clicked.connect(lambda _=False, k=spec.key: self._test(k))
            row = QHBoxLayout()
            row.addWidget(combo, 1)
            row.addWidget(test)
            form.addRow(spec.label, row)
            self._inputs[spec.key] = combo
        return box

    def _behaviour_box(self) -> QGroupBox:
        box = QGroupBox("السلوك والحدود")
        form = QFormLayout(box)
        for spec in settings.BEHAVIOUR_FIELDS:
            if spec.kind == "bool":
                widget: QWidget = QCheckBox(spec.help)
            else:
                widget = QLineEdit()
                widget.setToolTip(spec.help)
                widget.setPlaceholderText(spec.default)
            form.addRow(spec.label, widget)
            self._inputs[spec.key] = widget
        return box

    def _fixed_box(self) -> QGroupBox:
        box = QGroupBox("حاجات مش بتتغير من هنا")
        form = QFormLayout(box)
        try:
            ui = ThndrUiMap.from_toml(ui_map_path())
            calibrated = "complete" if ui.calibration_complete else "not complete"
            fence = "measured" if ui.submit_fence() else "not measured"
        except Exception as exc:  # noqa: BLE001
            calibrated = fence = f"config unreadable: {exc}"
        rows = (
            ("Mode", "live_read_only -- reads your account, never clicks or types. "
                     "Filling order tickets arrives in a later version."),
            ("Calibration", f"{calibrated}. Changed only after measuring the real screen."),
            ("Submit-button fence", f"{fence}. A coordinate on a real account is not a "
                                    "text box setting."),
            ("Strategy targets", "config/policy.egx.toml, under the no-overfit charter."),
        )
        for label, text in rows:
            value = QLabel(text)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(label, value)
        return box

    # ------------------------------------------------------------------ actions

    def reload(self) -> None:
        state = settings.load(store=self._store)
        for key, status in self._secret_status.items():
            source = state.secret_sources.get(key, "")
            status.setText("محفوظ" if source else "مش متسجل")
            status.setToolTip(f"saved in {source}" if source else "not set")
            theme.restyle_pill(status, "ok" if source else "muted")
            self._secret_inputs[key].clear()
        self._remove.clear()
        for key, widget in self._inputs.items():
            value = state.values.get(key, "")
            if isinstance(widget, QComboBox):
                # An editable combo shows its first suggestion when given "";
                # saving that would silently change an unset model.
                widget.setCurrentIndex(-1)
                widget.setEditText(value)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(value.lower() == "true")
            else:
                widget.setText(value)

    def _mark_remove(self, key: str) -> None:
        self._remove.add(key)
        self._secret_inputs[key].clear()
        self._secret_status[key].setText("هيتمسح لما تحفظ")
        theme.restyle_pill(self._secret_status[key], "bad")

    def _collect(self) -> dict[str, str]:
        values = {}
        for key, widget in self._inputs.items():
            if isinstance(widget, QComboBox):
                values[key] = widget.currentText().strip()
            elif isinstance(widget, QCheckBox):
                values[key] = "true" if widget.isChecked() else "false"
            else:
                values[key] = widget.text().strip()
        return values

    @Slot()
    def save(self) -> None:
        secrets = {k: e.text() for k, e in self._secret_inputs.items() if e.text().strip()}
        try:
            result = settings.save(
                self._collect(), secrets,
                remove_secrets=tuple(k for k in self._remove if k not in secrets),
                store=self._store,
            )
        except ValueError as exc:
            self._say(f"Not saved: {exc}", error=True)
            return
        except Exception as exc:  # noqa: BLE001 - credential store refused, disk full
            self._say(f"Not saved: {exc}", error=True)
            return
        # This process too: a Test right after Save must use the saved key.
        os.environ.update(settings.effective_values(store=self._store))
        note = "Saved."
        if result.secrets_in_env_file:
            note += " Keys went to .env because no credential store is available."
        self.reload()
        answer = QMessageBox.question(
            self, "EGX Robo-Advisor",
            "Saved. Restart the bot so the new settings take effect?\n\n"
            "The bot is halted first and has to be started again from the Dashboard.",
        )
        if answer == QMessageBox.Yes:
            self._on_saved()
            note += " The bot restarted halted."
        else:
            note += " Takes effect the next time the app starts."
        self._say(note)

    def _test(self, key: str) -> None:
        widget = self._inputs[key]
        model = widget.currentText().strip() if isinstance(widget, QComboBox) else ""
        self._say(f"Testing {model} ...")

        def run() -> None:
            # Keys typed but not yet saved are not used: test what will run.
            ok, message = settings.test_model(model)
            self._signals.finished.emit(model, ok, message)

        threading.Thread(target=run, daemon=True).start()

    @Slot(str, bool, str)
    def _show_test_result(self, model: str, ok: bool, message: str) -> None:
        self._say(f"{model}: {message}", error=not ok)

    def _say(self, text: str, *, error: bool = False) -> None:
        theme.say(self.message, text, "bad" if error else "ok")
