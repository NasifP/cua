"""The Learn tab: what the bot must learn before it may speculate, ticked as it learns.

The checkpoints and what earns each one are in egx_advisor/curriculum.py. A
tick comes only from evidence counted on this computer (curriculum.gather),
refreshed whenever the tab is shown; the model cannot tick anything.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import curriculum as cur
from .. import i18n
from ..i18n import tr
from . import theme
from .workers import run_async

MARKS = {"done": "✓", "learning": "○", "locked": "—"}
COLORS = {"done": "ok", "learning": "warn", "locked": "faint"}


def _lang() -> int:
    return 1 if i18n.current() == "ar" else 0


def describe(progress: Optional[cur.Progress]) -> str:
    """The numbers behind one checkpoint, in the current language."""
    if progress is None:
        return tr("learn.needs_dev")
    if progress.unit == "flag":
        return tr("learn.yes") if progress.done else tr("learn.not_yet")
    if progress.unit == "pct":
        text = tr("learn.edge", move=f"{progress.have:+.2f}", cost=f"{progress.need:.2f}")
        if progress.hit_rate is not None:
            text += "  ·  " + tr("learn.right", rate=f"{progress.hit_rate:.0%}")
        return text
    return tr("learn.count", have=f"{progress.have:g}", need=f"{progress.need:g}")


class LearnTab(QWidget):
    def __init__(self, gather: Callable[[], Any], remember: Callable[[Sequence[Any]], Any],
                 parent: Optional[QWidget] = None) -> None:
        """`gather()` -> curriculum.Evidence (off the UI thread); `remember(statuses)`
        -> {key: day first reached}."""
        super().__init__(parent)
        self.gather = gather
        self.remember = remember
        self.statuses: list[cur.Status] = []
        self._loading = False

        self.summary = QLabel()
        self.summary.setObjectName("PageSubtitle")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(220)
        self.refresh_button = QPushButton()
        self.refresh_button.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.bar)
        top.addWidget(self.refresh_button)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tree.currentItemChanged.connect(lambda item, _old: self._show_goal(item))

        self.goal = QLabel()
        self.goal.setWordWrap(True)
        self.goal.setObjectName("Hint")
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("Hint")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.goal)
        layout.addWidget(self.note)
        self.retranslate()

    # ------------------------------------------------------------------ data

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.summary.setText(tr("learn.checking"))
        gather, remember = self.gather, self.remember

        def work() -> tuple[list[cur.Status], dict[str, str]]:
            evidence = gather()
            days = remember(cur.evaluate(evidence)) or {}
            return cur.evaluate(evidence, days), days

        run_async(work, self._loaded, owner=self)

    def _loaded(self, result: Any, error: Optional[BaseException]) -> None:
        self._loading = False
        if error is not None:
            self.summary.setText(tr("learn.failed", error=f"{type(error).__name__}: {error}"))
            return
        self.show_statuses(result[0])

    # ------------------------------------------------------------------ view

    def show_statuses(self, statuses: Sequence[cur.Status]) -> None:
        """Fill the list (UI thread)."""
        self.statuses = list(statuses)
        lang = _lang()
        colors = theme.tokens()
        selected = self.tree.currentItem()
        selected_key = selected.data(0, Qt.UserRole) if selected else None
        self.tree.clear()
        for number, stage in enumerate(cur.STAGES, start=1):
            members = [s for s in self.statuses if s.checkpoint.stage == number]
            if not members:
                continue
            done = sum(1 for s in members if s.state == "done")
            head = QTreeWidgetItem(["", f"{number}. {stage[lang]}",
                                    f"{done}/{len(members)}", ""])
            font = head.font(1)
            font.setBold(True)
            for col in range(4):
                head.setFont(col, font)
            head.setFlags(Qt.ItemIsEnabled)
            self.tree.addTopLevelItem(head)
            for s in members:
                item = QTreeWidgetItem([MARKS[s.state], s.checkpoint.title[lang],
                                        describe(s.progress), s.since])
                item.setData(0, Qt.UserRole, s.checkpoint.key)
                brush = QBrush(QColor(colors[COLORS[s.state]]))
                item.setForeground(0, brush)
                if s.state == "locked":
                    item.setForeground(1, QBrush(QColor(colors["muted"])))
                item.setToolTip(1, s.checkpoint.goal[lang])
                self.tree.addTopLevelItem(item)
                if s.checkpoint.key == selected_key:
                    self.tree.setCurrentItem(item)
        done, measured = cur.summary(self.statuses)
        locked = sum(1 for s in self.statuses if s.state == "locked")
        self.summary.setText(tr("learn.summary", done=done, all=measured, locked=locked))
        self.bar.setRange(0, max(measured, 1))
        self.bar.setValue(done)
        self._show_goal(self.tree.currentItem())

    def _show_goal(self, item: Optional[QTreeWidgetItem]) -> None:
        key = item.data(0, Qt.UserRole) if item is not None else None
        found = next((s for s in self.statuses if s.checkpoint.key == key), None)
        self.goal.setText(found.checkpoint.goal[_lang()] if found else tr("learn.pick"))

    def retranslate(self) -> None:
        self.refresh_button.setText(tr("learn.refresh"))
        self.tree.setHeaderLabels(["", tr("learn.col.checkpoint"), tr("learn.col.progress"),
                                   tr("learn.col.since")])
        self.note.setText(tr("learn.note"))
        if self.statuses:
            self.show_statuses(self.statuses)
        else:
            self.summary.setText(tr("learn.checking"))
            self._show_goal(None)


def app_gather(memory: Any, archive: Any, open_bus: Callable[[], Any]) -> Callable[[], Any]:
    """The Evidence the running app has: memory, price archive, bus, config."""
    def gather() -> cur.Evidence:
        from ..paths import PROJECT_ROOT, config_path
        from ..scanner import load_universe

        try:
            universe = load_universe(config_path("scan_universe.toml"))
        except Exception:  # noqa: BLE001 - no list: nothing counts as priced
            universe = []
        try:
            bus = open_bus()
        except Exception:  # noqa: BLE001 - no bus: nothing learned from it
            bus = None
        try:
            return cur.gather(memory, archive, bus, universe, PROJECT_ROOT / "config")
        finally:
            if bus is not None:
                bus.close()
    return gather


def app_remember(memory: Any) -> Callable[[Sequence[Any]], Any]:
    return lambda statuses: cur.remember(memory, statuses, date.today().isoformat())
