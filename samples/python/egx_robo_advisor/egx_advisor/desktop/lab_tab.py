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

from PySide6.QtCore import QObject, Signal, Slot
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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..backtest.lab import LabReport, adopt, remove, run_lab
from ..paths import PROJECT_ROOT, config_path
from ..strategy.filters import RULE_KINDS, BuyFilter, load_rules

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
        self._signals = _Signals()
        self._signals.done.connect(self._finished)
        self._param_widgets: dict[str, QDoubleSpinBox] = {}

        intro = QLabel(
            "جرّب فكرة من أي موقع على تاريخ البورصة الحقيقي، بالعمولات. القاعدة تقدر "
            "بس تمنع شراء، ومش هتتعتمد إلا لو كسبت بعد التكاليف في نصّي التاريخ.\n"
            "Test an idea on real EGX history, with costs. A rule can only skip buys, "
            "and can be adopted only if it wins after costs in both halves."
        )
        intro.setWordWrap(True)

        self.kind = QComboBox()
        for kind, spec in RULE_KINDS.items():
            self.kind.addItem(spec["label"], kind)
        self.kind.currentIndexChanged.connect(self._rebuild_params)
        self.params_form = QFormLayout()
        self.years = QSpinBox()
        self.years.setRange(2, 20)
        self.years.setValue(8)
        self.years.setSuffix(" years")
        self.source = QComboBox()
        self.source.addItem("Real EGX prices (Yahoo)", False)
        self.source.addItem("Synthetic (checks the engine; never adoptable)", True)
        self.run_button = QPushButton("شغّل الاختبار  |  Run")
        self.run_button.clicked.connect(self.run)

        setup = QGroupBox("القاعدة  |  Rule")
        form = QFormLayout(setup)
        form.addRow("Rule", self.kind)
        form.addRow(self.params_form)
        form.addRow("History", self.years)
        form.addRow("Prices", self.source)
        form.addRow(self.run_button)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas, Menlo, monospace", 10))
        self.adopt_button = QPushButton("اعتمد القاعدة  |  Adopt")
        self.adopt_button.setEnabled(False)
        self.adopt_button.clicked.connect(self.adopt)

        self.adopted = QListWidget()
        remove_button = QPushButton("احذف المحدد  |  Remove selected")
        remove_button.clicked.connect(self.remove_selected)
        adopted_box = QGroupBox("قواعد معتمدة يستخدمها البوت  |  Rules the bot uses")
        adopted_layout = QVBoxLayout(adopted_box)
        adopted_layout.addWidget(self.adopted)
        adopted_layout.addWidget(remove_button)

        left = QVBoxLayout()
        left.addWidget(intro)
        left.addWidget(setup)
        left.addWidget(adopted_box, 1)
        right = QVBoxLayout()
        right.addWidget(self.output, 1)
        right.addWidget(self.adopt_button)
        layout = QHBoxLayout(self)
        layout.addLayout(left, 2)
        layout.addLayout(right, 3)

        self._rebuild_params()
        self._refresh_adopted()

    def _rebuild_params(self) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_widgets.clear()
        for name, (default, low, high) in RULE_KINDS[self.kind.currentData()]["params"].items():
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(0)
            box.setValue(default)
            self.params_form.addRow(name, box)
            self._param_widgets[name] = box

    def _rule(self) -> BuyFilter:
        return BuyFilter(self.kind.currentData(),
                         {k: w.value() for k, w in self._param_widgets.items()})

    @Slot()
    def run(self) -> None:
        rule = self._rule()
        years, synthetic = self.years.value(), bool(self.source.currentData())
        self.run_button.setEnabled(False)
        self.adopt_button.setEnabled(False)
        self.output.setPlainText(f"Fetching {years} years of prices and replaying ...")

        def work() -> None:
            try:
                history = load_history(years, synthetic)
                report = run_lab(history, [rule], synthetic=synthetic)
                self._signals.done.emit(report, "")
            except Exception as exc:  # noqa: BLE001 - network, data quality, too short
                self._signals.done.emit(None, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    @Slot(object, str)
    def _finished(self, report: Optional[LabReport], error: str) -> None:
        self.run_button.setEnabled(True)
        self._report = report
        if report is None:
            self.output.setPlainText("The test could not run:\n" + error)
            return
        self.output.setPlainText(report.render())
        self.adopt_button.setEnabled(report.passed)

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
        self.output.appendPlainText("\nAdopted. The bot applies it from its next cycle.")
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
            self.adopted.addItem(f"rules file unreadable: {exc}")
            return
        for a in rules:
            self.adopted.addItem(f"{a.rule.describe()}   (adopted {a.adopted_on}; {a.evidence})")
        if not rules:
            self.adopted.addItem("none yet")
