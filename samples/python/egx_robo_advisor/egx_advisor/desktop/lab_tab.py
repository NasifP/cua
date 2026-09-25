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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..backtest.lab import LabReport, adopt, remove, run_lab
from ..paths import PROJECT_ROOT, config_path
from ..strategy.filters import RULE_KINDS, BuyFilter, load_rules
from . import theme

RULES_FILE = config_path("rules.toml")

#: Arabic names for the rule kinds and their settings; RULE_KINDS keeps the English.
RULE_LABELS = {
    "sma": "منع الشراء لو السعر تحت متوسط N يوم",
    "rsi": "منع الشراء لو RSI فوق مستوى معيّن (تشبّع شراء)",
    "momentum": "منع الشراء بعد نزول حاد في N يوم",
}
PARAM_LABELS = {"days": "عدد الأيام", "above": "مستوى RSI", "fall_pct": "نسبة النزول %"}
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
            "القاعدة تقدر بس تمنع شراء، ومش هتتعتمد إلا لو كسبت بعد العمولات في التاريخ "
            "كله وفي كل نص منه لوحده."
        )
        intro.setToolTip("A rule can only skip buys, and can be adopted only if it wins "
                         "after costs over the whole history and in each half.")
        intro.setProperty("muted", "true")
        intro.setWordWrap(True)

        self.kind = QComboBox()
        for kind, spec in RULE_KINDS.items():
            self.kind.addItem(RULE_LABELS.get(kind, spec["label"]), kind)
            self.kind.setItemData(self.kind.count() - 1, spec["label"], Qt.ToolTipRole)
        self.kind.currentIndexChanged.connect(self._rebuild_params)
        self.params_form = QFormLayout()
        self.years = QSpinBox()
        self.years.setRange(2, 20)
        self.years.setValue(8)
        self.years.setSuffix(" سنين")
        self.source = QComboBox()
        self.source.addItem("أسعار البورصة الحقيقية (Yahoo)", False)
        self.source.addItem("أسعار وهمية: لاختبار البرنامج بس، ومش بتتعتمد", True)
        self.run_button = QPushButton("شغّل الاختبار")
        self.run_button.setProperty("variant", "primary")
        self.run_button.setToolTip("Replay the policy with and without the rule")
        self.run_button.clicked.connect(self.run)

        setup = QGroupBox("القاعدة")
        form = QFormLayout(setup)
        form.setSpacing(10)
        form.addRow(intro)
        form.addRow("القاعدة", self.kind)
        form.addRow(self.params_form)
        form.addRow("التاريخ", self.years)
        form.addRow("الأسعار", self.source)
        form.addRow(self.run_button)

        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas, Menlo, monospace", 10))
        self.output.setLayoutDirection(Qt.LeftToRight)  # the report is an English table
        self.adopt_button = QPushButton("اعتمد القاعدة")
        self.adopt_button.setProperty("variant", "primary")
        self.adopt_button.setToolTip("Enabled only for a rule that passed")
        self.adopt_button.setEnabled(False)
        self.adopt_button.clicked.connect(self.adopt)
        result_box = QGroupBox("النتيجة")
        result_layout = QVBoxLayout(result_box)
        result_layout.setSpacing(10)
        result_layout.addWidget(self.verdict)
        result_layout.addWidget(self.output, 1)
        result_layout.addWidget(self.adopt_button)

        self.adopted = QListWidget()
        remove_button = QPushButton("احذف المحددة")
        remove_button.setProperty("variant", "ghost")
        remove_button.clicked.connect(self.remove_selected)
        adopted_box = QGroupBox("القواعد اللي البوت بيستخدمها")
        adopted_layout = QVBoxLayout(adopted_box)
        adopted_layout.addWidget(self.adopted)
        adopted_layout.addWidget(remove_button, 0, Qt.AlignRight)

        left = QVBoxLayout()
        left.setSpacing(4)
        left.addWidget(setup)
        left.addWidget(adopted_box, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 20)
        layout.setSpacing(18)
        layout.addLayout(left, 2)
        layout.addWidget(result_box, 3)

        self._rebuild_params()
        self._refresh_adopted()
        theme.say(self.verdict, "اختار قاعدة واضغط \"شغّل الاختبار\". النتيجة هتظهر هنا: "
                  "العائد، وأقصى نزول، والعمولات، والحكم النهائي.", "info")

    def _rebuild_params(self) -> None:
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self._param_widgets.clear()
        for name, (default, low, high) in RULE_KINDS[self.kind.currentData()]["params"].items():
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(0)
            box.setValue(default)
            self.params_form.addRow(PARAM_LABELS.get(name, name), box)
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
        theme.say(self.verdict, "بيجرّب ... ممكن ياخد دقيقة أول مرة.", "info")

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
            theme.say(self.verdict, "الاختبار ماشتغلش. التفاصيل تحت.", "bad")
            return
        self.output.setPlainText(report.render())
        if report.passed:
            theme.say(self.verdict, "نجحت: كسبت بعد العمولات في التاريخ كله وفي كل نص. "
                      "تقدر تعتمدها.", "ok")
        else:
            theme.say(self.verdict, "مش هتتعتمد: " + report.verdict(), "bad")
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
        theme.say(self.verdict, "اتعتمدت. البوت هيطبّقها من الدورة الجاية.", "ok")
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
            self.adopted.addItem("لسه مفيش قواعد معتمدة")
