"""The Strategy Lab tab: test an indicator rule on real EGX history before using it.

Pick a rule and its settings (from TradingView, Investing.com, a course -- the
lab does not care where the idea came from), press Run, read the verdict.
Adopt is enabled only for a rule that passed; an adopted rule only ever skips
buys. See egx_advisor/backtest/lab.py for what "passed" means.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import date
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..backtest.lab import LabReport, adopt, remove, run_lab, run_strategy_comparison
from ..i18n import tr
from ..paths import PROJECT_ROOT, config_path
from ..strategy.filters import RULE_KINDS, BuyFilter, load_rules
from . import theme

RULES_FILE = config_path("rules.toml")

HISTORY_CACHE = PROJECT_ROOT / "state" / "history"


class _Signals(QObject):
    done = Signal(object, str)


def load_history(years: int, synthetic: bool) -> Any:
    """Real EGX closes from Yahoo (cached per span), or synthetic for an engine check."""
    from ..strategy.policy import AllocationPolicy

    universe = AllocationPolicy().universe
    if synthetic:
        from ..backtest import synthetic_history

        return synthetic_history([i.symbol for i in universe], start=date(2020, 1, 5),
                                 sessions=int(years * 245))
    from ..backtest import fetch_history
    from ..marketdata import YahooMarketData

    HISTORY_CACHE.mkdir(parents=True, exist_ok=True)
    history, _coverage, _quality = asyncio.run(fetch_history(
        YahooMarketData(), universe, days=int(years * 365),
        cache=HISTORY_CACHE / f"egx_{years}y.csv",
    ))
    return history


class LabTab(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report: Optional[LabReport] = None
        self._error = ""
        #: The banner as (key, kind, values), so a language switch can redraw it.
        self._banner: tuple[str, str, dict] = ("lab.hint", "info", {})
        self._signals = _Signals()
        self._signals.done.connect(self._finished)
        self._param_widgets: dict[str, QDoubleSpinBox] = {}

        self.intro = QLabel()
        self.intro.setProperty("muted", "true")
        self.intro.setWordWrap(True)

        self.kind = QComboBox()
        for kind, spec in RULE_KINDS.items():
            self.kind.addItem(spec["label"], kind)
        self.kind.currentIndexChanged.connect(self._rebuild_params)
        self.params_form = QFormLayout()
        self.years = QSpinBox()
        self.years.setRange(2, 20)
        self.years.setValue(8)
        self.source = QComboBox()
        self.source.addItem("", False)
        self.source.addItem("", True)
        self.run_button = QPushButton()
        self.run_button.setProperty("variant", "primary")
        self.run_button.clicked.connect(self.run)
        self.compare_button = QPushButton()
        self.compare_button.clicked.connect(self.compare)

        self.setup = QGroupBox()
        self.form = QFormLayout(self.setup)
        self.form.setSpacing(10)
        self.form.addRow(self.intro)
        # Labels kept by hand: addRow("") makes no label to translate later.
        self._row_labels = {key: QLabel() for key in ("lab.rule", "lab.history", "lab.prices")}
        self.form.addRow(self._row_labels["lab.rule"], self.kind)
        self.form.addRow(self.params_form)
        self.form.addRow(self._row_labels["lab.history"], self.years)
        self.form.addRow(self._row_labels["lab.prices"], self.source)
        self.form.addRow(self.run_button)
        self.form.addRow(self.compare_button)

        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas, Menlo, monospace", 10))
        self.adopt_button = QPushButton()
        self.adopt_button.setProperty("variant", "primary")
        self.adopt_button.setEnabled(False)
        self.adopt_button.clicked.connect(self.adopt)
        self.result_box = QGroupBox()
        result_layout = QVBoxLayout(self.result_box)
        result_layout.setSpacing(10)
        result_layout.addWidget(self.verdict)
        result_layout.addWidget(self.output, 1)
        result_layout.addWidget(self.adopt_button)

        self.adopted = QListWidget()
        self.remove_button = QPushButton()
        self.remove_button.setProperty("variant", "ghost")
        self.remove_button.clicked.connect(self.remove_selected)
        self.adopted_box = QGroupBox()
        adopted_layout = QVBoxLayout(self.adopted_box)
        adopted_layout.addWidget(self.adopted)
        adopted_layout.addWidget(self.remove_button, 0, Qt.AlignRight)

        # The rule form is tall (six settings for the four-factor rule): on a
        # short screen it scrolls instead of squeezing the rows together.
        left_panel = QWidget()
        left = QVBoxLayout(left_panel)
        left.setContentsMargins(0, 0, 6, 0)
        left.setSpacing(4)
        left.addWidget(self.setup)
        left.addWidget(self.adopted_box, 1)
        self.adopted.setMinimumHeight(90)
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setWidget(left_panel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.setSpacing(18)
        layout.addWidget(left_scroll, 2)
        layout.addWidget(self.result_box, 3)

        self.retranslate()

    def retranslate(self) -> None:
        self.intro.setText(tr("lab.intro"))
        for index in range(self.kind.count()):
            kind = self.kind.itemData(index)
            self.kind.setItemText(index, tr(f"lab.kind.{kind}", RULE_KINDS[kind]["label"]))
        self.years.setSuffix(tr("lab.years"))
        self.source.setItemText(0, tr("lab.real"))
        self.source.setItemText(1, tr("lab.synthetic"))
        self.run_button.setText(tr("lab.run"))
        self.run_button.setToolTip(tr("lab.run_tip"))
        self.compare_button.setText(tr("lab.compare"))
        self.compare_button.setToolTip(tr("lab.compare_tip"))
        self.setup.setTitle(tr("lab.rule_box"))
        for key, label in self._row_labels.items():
            label.setText(tr(key))
        self.result_box.setTitle(tr("lab.result_box"))
        self.adopt_button.setText(tr("lab.adopt"))
        self.adopt_button.setToolTip(tr("lab.adopt_tip"))
        self.adopted_box.setTitle(tr("lab.adopted_box"))
        self.remove_button.setText(tr("lab.remove"))
        # The report is a table of figures: it reads left to right in either language.
        self.output.setLayoutDirection(Qt.LeftToRight)
        self._relabel_params()
        self._refresh_adopted()
        self._show_result()

    def _say(self, key: str, kind: str, **values: object) -> None:
        self._banner = (key, kind, values)
        theme.say(self.verdict, tr(key, **values), kind)

    def _show_result(self) -> None:
        if self._report is not None:
            self.output.setPlainText(self._report.render(i18n.current()))
        elif self._error:
            self.output.setPlainText(self._error)
        key, kind, values = self._banner
        if key in ("lab.not_adopted", "lab.strategy_failed") and self._report is not None:
            values = {"verdict": self._report.verdict(i18n.current())}
        self._say(key, kind, **values)

    def _rebuild_params(self) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_widgets.clear()
        for name, (default, low, high) in RULE_KINDS[self.kind.currentData()]["params"].items():
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(0)
            box.setValue(default)
            self.params_form.addRow(tr(f"lab.param.{name}", name), box)
            self._param_widgets[name] = box

    def _relabel_params(self) -> None:
        if not self._param_widgets:
            self._rebuild_params()
            return
        for name, box in self._param_widgets.items():
            label = self.params_form.labelForField(box)
            if label is not None:
                label.setText(tr(f"lab.param.{name}", name))

    def _rule(self) -> BuyFilter:
        return BuyFilter(self.kind.currentData(),
                         {k: w.value() for k, w in self._param_widgets.items()})

    @Slot()
    def run(self) -> None:
        rule = self._rule()
        self._start(lambda history, synthetic: run_lab(history, [rule], synthetic=synthetic))

    @Slot()
    def compare(self) -> None:
        """The four-factor strategy against the current plan; read, never adopted."""
        self._start(lambda history, synthetic: run_strategy_comparison(
            history, synthetic=synthetic))

    def _start(self, test) -> None:
        years, synthetic = self.years.value(), bool(self.source.currentData())
        self.run_button.setEnabled(False)
        self.compare_button.setEnabled(False)
        self.adopt_button.setEnabled(False)
        self._report, self._error = None, ""
        self.output.setPlainText(tr("lab.fetching", years=years))
        self._say("lab.running", "info")

        def work() -> None:
            try:
                history = load_history(years, synthetic)
                report = test(history, synthetic)
                self._signals.done.emit(report, "")
            except Exception as exc:  # noqa: BLE001 - network, data quality, too short
                self._signals.done.emit(None, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    @Slot(object, str)
    def _finished(self, report: Optional[LabReport], error: str) -> None:
        self.run_button.setEnabled(True)
        self.compare_button.setEnabled(True)
        self._report, self._error = report, error
        strategy = report is not None and not report.rules
        if report is None:
            self._banner = ("lab.could_not_run", "bad", {})
        elif strategy:
            self._banner = (("lab.strategy_passed", "ok", {}) if report.passed
                            else ("lab.strategy_failed", "bad", {}))
        elif report.passed:
            self._banner = ("lab.passed", "ok", {})
        else:
            self._banner = ("lab.not_adopted", "bad", {})
        self._show_result()
        # A strategy is switched on in Settings; only rules are adopted here.
        self.adopt_button.setEnabled(report is not None and report.passed and not strategy)

    @Slot()
    def adopt(self) -> None:
        if self._report is None:
            return
        try:
            adopt(self._report, RULES_FILE, date.today())
        except ValueError as exc:
            self.output.appendPlainText(f"\n{exc}")
            return
        self.adopt_button.setEnabled(False)
        self._say("lab.adopted", "ok")
        self._refresh_adopted()

    @Slot()
    def remove_selected(self) -> None:
        row = self.adopted.currentRow()
        rules = load_rules(RULES_FILE)
        if 0 <= row < len(rules):
            remove(rules[row].rule, RULES_FILE)
            self._refresh_adopted()

    def _refresh_adopted(self) -> None:
        self.adopted.clear()
        try:
            rules = load_rules(RULES_FILE)
        except Exception as exc:  # noqa: BLE001
            self.adopted.addItem(tr("lab.unreadable", error=exc))
            return
        for a in rules:
            self.adopted.addItem(tr("lab.adopted_item", rule=a.rule.describe(i18n.current()),
                                    day=a.adopted_on, evidence=a.evidence))
        if not rules:
            self.adopted.addItem(tr("lab.none"))
