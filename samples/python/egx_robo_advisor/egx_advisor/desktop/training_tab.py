"""The Training tab: what the operator teaches the app, in one place.

- Indicators: which indicators the operator uses and their settings. Saving
  puts them on the Chart tab; the analyst reads them for every stock it
  reviews; "Study on the EGX" measures what followed each indicator's signals
  on every stock in the scan list and keeps the result in memory, where the
  analyst finds it (indicators.py).
- Ticket boxes: the two Thndr X order-ticket boxes the ticket panel fills
  (teach_box.py). They used to be taught from the panel beside the browser.
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import indicators as ind
from ..i18n import tr
from ..memory import Memory
from . import theme
from .sources_tab import _table
from .teach_box import TeachBox

HistoryFn = Callable[[], Mapping[str, Sequence[Any]]]
#: About three years of sessions for a study.
STUDY_DAYS = 1100


def universe_history(archive: Any) -> HistoryFn:
    """Daily bars for every stock in the scan list, through the price archive."""
    def fetch() -> Mapping[str, Sequence[Any]]:
        from ..analyst.tools import yahoo_history
        from ..paths import config_path
        from ..scanner import load_universe

        symbols = load_universe(config_path("scan_universe.toml"))
        history = yahoo_history(archive)(symbols, STUDY_DAYS)
        today = date.today()
        return {s: sorted((r for r in rows if r.day < today), key=lambda r: r.day)
                for s, rows in history.items() if rows}
    return fetch


class _Signals(QObject):
    studied = Signal(object, str)


class TrainingTab(QWidget):
    def __init__(self, memory: Memory, page: Callable[[], QWebEnginePage],
                 archive: Optional[Any] = None, history: Optional[HistoryFn] = None,
                 on_apply: Callable[[list[str]], None] = lambda studies: None,
                 on_taught: Callable[[], None] = lambda: None,
                 on_teach_start: Callable[[], None] = lambda: None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.memory = memory
        self._history = history or universe_history(archive)
        self._on_apply = on_apply
        self._signals = _Signals()
        self._signals.studied.connect(self._studied)
        self.choices = ind.load_set(memory.ui_get(Memory.INDICATORS_KEY))
        self.studying = False

        # --- indicators ---
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("muted", "true")
        self.table = _table(3)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.SelectedClicked
                                   | QAbstractItemView.EditKeyPressed)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.save_button = QPushButton()
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        self.study_button = QPushButton()
        self.study_button.setProperty("variant", "primary")
        self.study_button.clicked.connect(self.study)
        self.reset_button = QPushButton()
        self.reset_button.setProperty("variant", "ghost")
        self.reset_button.clicked.connect(self.reset)
        buttons = QHBoxLayout()
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.study_button)
        self.results_title = QLabel()
        self.results_title.setObjectName("SectionTitle")
        self.results = _table(7)
        self.results_hint = QLabel()
        self.results_hint.setWordWrap(True)
        self.results_hint.setProperty("muted", "true")
        self.message = QLabel("")
        self.message.setWordWrap(True)
        indicators_page = QWidget()
        layout = QVBoxLayout(indicators_page)
        layout.addWidget(self.hint)
        layout.addWidget(self.table, 2)
        layout.addLayout(buttons)
        layout.addWidget(self.message)
        layout.addWidget(self.results_title)
        layout.addWidget(self.results, 2)
        layout.addWidget(self.results_hint)

        # --- ticket boxes ---
        self.teach_box = TeachBox(page=page, on_taught=on_taught, on_start=on_teach_start)
        ticket_page = QWidget()
        ticket_layout = QVBoxLayout(ticket_page)
        ticket_layout.addWidget(self.teach_box)
        ticket_layout.addStretch(1)

        self.tabs = QTabWidget()
        self.tabs.addTab(indicators_page, "")
        self.tabs.addTab(ticket_page, "")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 20)
        outer.addWidget(self.tabs)
        self.retranslate()

    # ------------------------------------------------------------------ views

    def retranslate(self) -> None:
        self.tabs.setTabText(0, tr("train.indicators"))
        self.tabs.setTabText(1, tr("train.ticket"))
        self.hint.setText(tr("train.hint"))
        self.table.setHorizontalHeaderLabels(
            [tr("train.col_indicator"), tr("train.col_settings"), tr("train.col_names")])
        self.save_button.setText(tr("train.save"))
        self.study_button.setText(tr("train.study"))
        self.reset_button.setText(tr("train.reset"))
        self.results_title.setText(tr("train.results"))
        self.results.setHorizontalHeaderLabels(
            [tr("train.col_indicator"), tr("train.col_signals"), tr("train.col_hit"),
             tr("train.col_avg"), tr("train.col_market"), tr("train.col_verdict"),
             tr("train.col_best")])
        self.results_hint.setText(tr("train.results_hint", n=ind.MIN_SIGNALS,
                                     edge=ind.EDGE_PCT))
        self.teach_box.retranslate()
        self._paint_table()
        self._paint_results()

    def _paint_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.choices))
        for row, choice in enumerate(self.choices):
            spec = ind.CATALOG[choice.key]
            name = QTableWidgetItem(tr(f"ind.{choice.key}"))
            name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            name.setCheckState(Qt.Checked if choice.on else Qt.Unchecked)
            name.setData(Qt.UserRole, choice.key)
            name.setToolTip(tr(f"ind.{choice.key}.tip"))
            # Isolated left-to-right, so "9, 25, 75" is not reversed in Arabic.
            values = QTableWidgetItem(_ltr(", ".join(ind._fmt(choice.value(n))
                                                     for n, _ in spec.params)))
            values.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable | Qt.ItemIsSelectable)
            names = QTableWidgetItem(", ".join(tr(f"ind.param.{n}") for n, _ in spec.params))
            names.setFlags(Qt.ItemIsEnabled)
            for col, item in enumerate((name, values, names)):
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)

    def _paint_results(self) -> None:
        rows = [r for r in self.memory.indicator_studies() if r.get("key") in ind.CATALOG]
        self.results.setRowCount(len(rows))
        far = max(ind.HORIZONS)
        for row, r in enumerate(rows):
            h = r.get(f"h{far}") or {}
            best = ", ".join(b["symbol"].removesuffix(".CA") for b in r.get("best_symbols", [])[:3])
            cells = (
                f"{tr('ind.' + r['key'])} {_ltr(r['indicator'][len(r['key']):])}",
                str(h.get("buy_signals", 0)),
                _pct(h.get("buy_hit_pct"), signed=False),
                _pct(h.get("buy_avg_pct")),
                _pct(h.get("market_avg_pct")),
                tr(f"train.verdict.{r.get('verdict', 'too_few')}"),
                best or "-",
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 5:
                    item.setToolTip(tr("train.studied_on", day=str(r.get("studied", ""))[:10]))
                self.results.setItem(row, col, item)

    def _say(self, text: str, kind: str = "ok") -> None:
        theme.say(self.message, text, kind)

    # ------------------------------------------------------------------ actions

    def read_table(self) -> list[ind.Choice]:
        """The set as edited; raises ValueError naming the first bad setting."""
        out = []
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 0).data(Qt.UserRole)
            on = self.table.item(row, 0).checkState() == Qt.Checked
            names = [n for n, _ in ind.CATALOG[key].params]
            text = self.table.item(row, 1).text().translate(_UNMARK).replace("،", ",")
            raw = [x.strip() for x in text.split(",")]
            if len(raw) != len(names):
                raise ValueError(tr("train.bad_count", indicator=tr(f"ind.{key}"),
                                    n=len(names)))
            out.append(ind.Choice(key, on, ind.check_params(key, dict(zip(names, raw)))))
        return out

    @Slot()
    def save(self) -> bool:
        try:
            self.choices = self.read_table()
        except ValueError as exc:
            self._say(str(exc), "bad")
            return False
        self.memory.ui_set(Memory.INDICATORS_KEY, ind.dump_set(self.choices))
        self._on_apply(ind.tv_studies(self.choices))
        self._paint_table()
        self._say(tr("train.saved", n=sum(c.on for c in self.choices)))
        return True

    @Slot()
    def reset(self) -> None:
        self.choices = ind.default_set()
        self._paint_table()
        self._say(tr("train.reset_done"), "info")

    @Slot()
    def study(self) -> None:
        if self.studying or not self.save():
            return
        chosen = [c for c in self.choices if c.on]
        if not chosen:
            self._say(tr("train.none_on"), "bad")
            return
        self.studying = True
        self.study_button.setEnabled(False)
        self._say(tr("train.studying"), "info")
        history_fn = self._history

        def work() -> None:
            try:
                history = history_fn()
                if not history:
                    raise RuntimeError(tr("train.no_data"))
                results = [ind.study(c, history) for c in chosen]
                self._signals.studied.emit(results, "")
            except Exception as exc:  # noqa: BLE001 - network, data
                self._signals.studied.emit([], f"{type(exc).__name__}: {exc}")

        threading.Thread(target=work, daemon=True).start()

    @Slot(object, str)
    def _studied(self, results: list, problem: str) -> None:
        self.studying = False
        self.study_button.setEnabled(True)
        if problem:
            self._say(problem, "bad")
            return
        for result in results:
            self.memory.add_indicator_study(result)
        helped = [r for r in results if r["verdict"] == "helped"]
        self._say(tr("train.studied", n=len(results), helped=len(helped),
                     symbols=results[0]["symbols"] if results else 0))
        self._paint_results()


#: Direction marks and Arabic digits, as someone may type or paste them.
_UNMARK = str.maketrans({"⁦": None, "⁧": None, "⁨": None, "⁩": None,
                         "‎": None, "‏": None, **{a: str(i) for i, a in
                                                           enumerate("٠١٢٣٤٥٦٧٨٩")},
                         "٫": "."})


def _ltr(text: str) -> str:
    return "⁦" + text + "⁩"


def _pct(value: Optional[float], signed: bool = True) -> str:
    if value is None:
        return "-"
    text = f"{value:+.1f}%" if signed else f"{value:.0f}%"
    return "⁦" + text + "⁩"  # keeps the sign on the left in Arabic
