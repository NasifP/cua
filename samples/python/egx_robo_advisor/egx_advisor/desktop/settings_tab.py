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
from ..i18n import tr
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
        self._texts: list[Callable[[], None]] = []

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

        self.save_button = QPushButton()
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
        self._text(lambda: self.save_button.setText(tr("set.save")))
        self.retranslate()

    # ------------------------------------------------------------------ building

    def _text(self, apply: Callable[[], None]) -> None:
        """Register a text setter; retranslate() runs them all again."""
        self._texts.append(apply)

    def retranslate(self) -> None:
        """Relabel in the current language. Values typed but not saved are kept."""
        first = not hasattr(self, "_sources")
        for apply in self._texts:
            apply()
        if first:
            self.reload()
        else:
            self._paint_statuses()
        self._say("")

    @staticmethod
    def _field(key: str, fallback: str) -> str:
        return tr(f"field.{key}", fallback)

    def _keys_box(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        if not self._store.available:
            where_key = "set.where_env"
        elif os.name == "nt":
            where_key = "set.where_windows"
        else:
            where_key = "set.where_keyring"
        note = QLabel()
        note.setProperty("muted", "true")
        note.setWordWrap(True)
        form.addRow(note)
        self._text(lambda: box.setTitle(tr("set.keys")))
        self._text(lambda: note.setText(tr("set.keys_note", where=tr(where_key))))
        for spec in settings.SECRET_FIELDS:
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.Password)
            status = QLabel("")
            status.setMinimumWidth(90)
            status.setAlignment(Qt.AlignCenter)
            remove = QPushButton()
            remove.setProperty("variant", "ghost")
            remove.clicked.connect(lambda _=False, k=spec.key: self._mark_remove(k))
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(status)
            row.addWidget(remove)
            form.addRow(spec.label, row)
            label = form.labelForField(row)

            def texts(spec=spec, edit=edit, remove=remove, label=label) -> None:
                edit.setPlaceholderText(tr(f"field.{spec.key}.help", spec.help))
                remove.setText(tr("set.remove"))
                remove.setToolTip(tr("set.remove_tip", key=spec.key))
                label.setText(self._field(spec.key, spec.label))

            self._text(texts)
            self._secret_inputs[spec.key] = edit
            self._secret_status[spec.key] = status
        return box

    def _models_box(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        note = QLabel()
        note.setWordWrap(True)
        note.setProperty("muted", "true")
        form.addRow(note)
        self._text(lambda: box.setTitle(tr("set.models")))
        self._text(lambda: note.setText(tr("set.models_note")))
        for spec in settings.MODEL_FIELDS:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(list(spec.suggestions))
            combo.setToolTip(spec.help)
            # Model ids read left to right in either language.
            combo.setLayoutDirection(Qt.LeftToRight)
            test = QPushButton()
            test.clicked.connect(lambda _=False, k=spec.key: self._test(k))
            row = QHBoxLayout()
            row.addWidget(combo, 1)
            row.addWidget(test)
            form.addRow(spec.label, row)
            label = form.labelForField(row)

            def texts(spec=spec, combo=combo, test=test, label=label) -> None:
                if not spec.default:
                    combo.lineEdit().setPlaceholderText(tr("set.same_as_chat"))
                test.setText(tr("set.test"))
                test.setToolTip(tr("set.test_tip"))
                label.setText(self._field(spec.key, spec.label))

            self._text(texts)
            self._inputs[spec.key] = combo
        return box

    def _behaviour_box(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        self._text(lambda: box.setTitle(tr("set.behaviour")))
        for spec in settings.BEHAVIOUR_FIELDS:
            if spec.kind == "bool":
                widget: QWidget = QCheckBox()
            else:
                widget = QLineEdit()
                widget.setPlaceholderText(spec.default)
            form.addRow(spec.label, widget)
            label = form.labelForField(widget)

            def texts(spec=spec, widget=widget, label=label) -> None:
                help_text = tr(f"field.{spec.key}.help", spec.help)
                if isinstance(widget, QCheckBox):
                    widget.setText(help_text)
                else:
                    widget.setToolTip(help_text)
                label.setText(self._field(spec.key, spec.label))

            self._text(texts)
            self._inputs[spec.key] = widget
        return box

    def _fixed_box(self) -> QGroupBox:
        box = QGroupBox()
        form = QFormLayout(box)
        self._text(lambda: box.setTitle(tr("set.fixed")))
        try:
            ui = ThndrUiMap.from_toml(ui_map_path())
            calibrated_key = ("set.fixed.complete" if ui.calibration_complete
                              else "set.fixed.incomplete")
            fence_key = "set.fixed.measured" if ui.submit_fence() else "set.fixed.unmeasured"
            error = ""
        except Exception as exc:  # noqa: BLE001
            calibrated_key = fence_key = "set.fixed.unreadable"
            error = str(exc)

        def calibration() -> str:
            return tr("set.fixed.calibration_text", state=tr(calibrated_key, error=error))

        def fence() -> str:
            return tr("set.fixed.fence_text", state=tr(fence_key, error=error))

        rows = (
            ("set.fixed.mode", lambda: tr("set.fixed.mode_text")),
            ("set.fixed.calibration", calibration),
            ("set.fixed.fence", fence),
            ("set.fixed.targets", lambda: tr("set.fixed.targets_text")),
        )
        for label_key, text in rows:
            value = QLabel()
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label = QLabel()
            form.addRow(label, value)

            def texts(value=value, label=label, label_key=label_key, text=text) -> None:
                value.setText(text())
                label.setText(tr(label_key))

            self._text(texts)
        return box

    # ------------------------------------------------------------------ actions

    def reload(self) -> None:
        state = settings.load(store=self._store)
        self._sources = dict(state.secret_sources)
        self._remove.clear()
        for edit in self._secret_inputs.values():
            edit.clear()
        self._paint_statuses()
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

    def _paint_statuses(self) -> None:
        for key, status in self._secret_status.items():
            source = self._sources.get(key, "")
            if key in self._remove:
                status.setText(tr("set.will_remove"))
                theme.restyle_pill(status, "bad")
                continue
            status.setText(tr("set.saved_pill") if source else tr("set.not_set"))
            status.setToolTip(source or "")
            theme.restyle_pill(status, "ok" if source else "muted")

    def _mark_remove(self, key: str) -> None:
        self._remove.add(key)
        self._secret_inputs[key].clear()
        self._secret_status[key].setText(tr("set.will_remove"))
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
            self._say(tr("set.not_saved", error=exc), error=True)
            return
        except Exception as exc:  # noqa: BLE001 - credential store refused, disk full
            self._say(tr("set.not_saved", error=exc), error=True)
            return
        # This process too: a Test right after Save must use the saved key.
        os.environ.update(settings.effective_values(store=self._store))
        note = tr("set.saved")
        if result.secrets_in_env_file:
            note += tr("set.keys_in_env")
        self.reload()
        answer = QMessageBox.question(
            self, "EGX Robo-Advisor",
            tr("set.restart_q"),
        )
        if answer == QMessageBox.Yes:
            self._on_saved()
            note += tr("set.restarted")
        else:
            note += tr("set.next_start")
        self._say(note)

    def _test(self, key: str) -> None:
        widget = self._inputs[key]
        model = widget.currentText().strip() if isinstance(widget, QComboBox) else ""
        self._say(tr("set.testing", model=model))

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
