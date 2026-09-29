"""The Paper trading page: the journal of virtual trades, and the loss limits.

The bot opens a paper trade for every buy its plan wants (egx_cua_agent.py);
the operator can open one here too. Refresh fetches the latest daily prices,
closes what reached its stop or target (paper.py) and shows the result. No
money moves and nothing reaches Thndr X.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping, Optional, Sequence

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import paper as pp
from ..i18n import tr
from . import theme
from .sources_tab import _table
from .workers import run_async

HistoryFn = Callable[[Sequence[str], int], Mapping[str, Sequence[Any]]]
#: Sessions fetched for stops (ATR) and for settling open trades.
HISTORY_DAYS = 200


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _symbol(text: str) -> str:
    text = text.strip().upper()
    return text if not text or text.endswith(".CA") or text.startswith("^") else text + ".CA"


class PaperTab(QWidget):
    def __init__(self, book: Callable[[], pp.PaperBook], history: HistoryFn,
                 style: Callable[[], str], parent: Optional[QWidget] = None) -> None:
        """`book()` -> PaperBook (settings are read at each call); `history(symbols, days)`
        -> daily bars; `style()` -> EGX_STYLE. All called off the UI thread."""
        super().__init__(parent)
        self.book, self.history, self.style = book, history, style
        self.snapshot: dict[str, Any] = {}
        self._loading = False

        self.summary = QLabel()
        self.summary.setObjectName("PageSubtitle")
        self.summary.setWordWrap(True)
        self.state = QLabel()
        self.resume_button = QPushButton()
        self.resume_button.clicked.connect(self._resume)
        self.refresh_button = QPushButton()
        self.refresh_button.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.state)
        top.addWidget(self.resume_button)
        top.addWidget(self.refresh_button)

        self.ticker = QLineEdit()
        self.ticker.setMaximumWidth(160)
        self.ticker.returnPressed.connect(self._open)
        self.open_button = QPushButton()
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self._open)
        self.message = QLabel()
        self.message.setWordWrap(True)
        form = QHBoxLayout()
        form.addWidget(self.ticker)
        form.addWidget(self.open_button)
        form.addWidget(self.message, 1)

        self.open_label = QLabel()
        self.open_table = _table(8)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self._close_selected)
        open_head = QHBoxLayout()
        open_head.addWidget(self.open_label, 1)
        open_head.addWidget(self.close_button)
        self.closed_label = QLabel()
        self.closed_table = _table(8)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setObjectName("Hint")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(10)
        layout.addLayout(top)
        layout.addLayout(form)
        layout.addLayout(open_head)
        layout.addWidget(self.open_table, 2)
        layout.addWidget(self.closed_label)
        layout.addWidget(self.closed_table, 3)
        layout.addWidget(self.note)
        self.retranslate()

    # ------------------------------------------------------------------ work

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh()

    def _bars(self, symbols: Sequence[str]) -> dict[str, list[Any]]:
        if not symbols:
            return {}
        today = date.today()
        rows = self.history(list(symbols), HISTORY_DAYS)
        return {s: sorted((r for r in rows.get(s) or () if r.day < today),
                          key=lambda r: r.day) for s in symbols}

    def _read(self) -> dict[str, Any]:
        """Settle with fresh prices, then everything the page shows. Off the UI thread."""
        book = self.book()
        today = date.today()
        bars = self._bars(sorted({t.symbol for t in book.trades("open")}))
        _closed, breach = book.settle(lambda s: bars.get(s, []), today)
        marks = {s: float(rows[-1].close) for s, rows in bars.items() if rows}
        return {"trades": book.trades(), "stats": book.stats(marks), "marks": marks,
                "paused": book.paused(today), "breach": breach, "guard": book.guard()}

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.state.setText(tr("paper.checking"))
        run_async(self._read, self._loaded, owner=self)

    def _loaded(self, result: Any, error: Optional[BaseException]) -> None:
        self._loading = False
        if error is not None:
            theme.say(self.message, tr("paper.failed", error=f"{type(error).__name__}: {error}"),
                      "bad")
            self.state.setText("")
            return
        self.show_snapshot(result)

    def _open(self) -> None:
        symbol = _symbol(self.ticker.text())
        if not symbol:
            theme.say(self.message, tr("paper.need_symbol"), "bad")
            return
        theme.say(self.message, tr("paper.opening"), "info")
        book, style, bars_of = self.book, self.style, self._bars

        def work() -> pp.Trade:
            rows = bars_of([symbol]).get(symbol) or []
            if not rows:
                raise ValueError(f"no prices for {symbol}")
            return pp.open_from_levels(book(), symbol, rows, float(rows[-1].close),
                                       style=style(), source="you", reason="opened by hand",
                                       today=date.today())

        run_async(work, self._opened, owner=self)

    def _opened(self, trade: Optional[pp.Trade], error: Optional[BaseException]) -> None:
        if error is not None:
            theme.say(self.message, str(error), "bad")
            return
        theme.say(self.message, tr("paper.opened", qty=trade.quantity, symbol=trade.symbol,
                                   entry=f"{trade.entry:g}", stop=f"{trade.stop:.2f}",
                                   target=f"{trade.target:.2f}"), "ok")
        self.ticker.clear()
        self.refresh()

    def _selected_open(self) -> Optional[pp.Trade]:
        row = self.open_table.currentRow()
        trades = [t for t in self.snapshot.get("trades", []) if t.status == "open"]
        return trades[row] if 0 <= row < len(trades) else None

    def _close_selected(self) -> None:
        trade = self._selected_open()
        if trade is None:
            theme.say(self.message, tr("paper.pick_open"), "bad")
            return
        price = self.snapshot.get("marks", {}).get(trade.symbol)
        if price is None:
            theme.say(self.message, tr("paper.no_price"), "bad")
            return
        book = self.book
        run_async(lambda: book().close_by_hand(trade.id, price, date.today()),
                  lambda _r, e: (theme.say(self.message, str(e), "bad") if e
                                 else self.refresh()), owner=self)

    def _resume(self) -> None:
        book, marks = self.book, dict(self.snapshot.get("marks", {}))
        run_async(lambda: book().resume(date.today(), marks),
                  lambda _r, e: (theme.say(self.message, str(e), "bad") if e
                                 else self.refresh()), owner=self)

    # ------------------------------------------------------------------ view

    def show_snapshot(self, snap: Mapping[str, Any]) -> None:
        """Fill the page (UI thread)."""
        self.snapshot = dict(snap)
        stats: pp.Stats = snap["stats"]
        marks = snap.get("marks", {})
        guard = snap.get("guard") or {}
        self.summary.setText(tr(
            "paper.summary", equity=_money(stats.equity), capital=_money(stats.capital),
            realised=f"{stats.realised:+,.0f}", open=f"{stats.open_pnl:+,.0f}",
            # The guard keeps the day's loss; shown as the day's change (no "-0.0").
            today=f"{0.0 - float(guard.get('daily_pct', 0)):+.1f}",
            drawdown=f"{stats.drawdown_pct:.1f}", closed=stats.closed,
            rate=f"{stats.win_rate:.0%}"))
        paused = snap.get("paused")
        if paused:
            theme.restyle_pill(self.state, "bad")
            self.state.setText(tr(f"paper.paused.{paused}"))
        else:
            theme.restyle_pill(self.state, "ok")
            self.state.setText(tr("paper.active"))
        self.resume_button.setVisible(paused == "drawdown")
        breach = snap.get("breach")
        if breach is not None:
            theme.say(self.message, tr("paper.breach", what=breach.describe()), "bad")

        why = {"stop": tr("paper.why.stop"), "target": tr("paper.why.target"),
               "manual": tr("paper.why.manual")}
        trades = snap.get("trades", [])
        open_rows = []
        for t in (t for t in trades if t.status == "open"):
            last = marks.get(t.symbol)
            open_rows.append((t.symbol.removesuffix(".CA"), t.opened.isoformat(), t.quantity,
                              f"{t.entry:g}", f"{t.stop:.2f}", f"{t.target:.2f}",
                              "-" if last is None else f"{last:g}", f"{t.pnl(last):+,.0f}"))
        closed_rows = [(t.symbol.removesuffix(".CA"), t.opened.isoformat(),
                        t.closed.isoformat() if t.closed else "", t.quantity, f"{t.entry:g}",
                        f"{t.exit:g}" if t.exit is not None else "",
                        why.get(t.exit_reason or "", t.exit_reason or ""), f"{t.pnl():+,.0f}")
                       for t in reversed([t for t in trades if t.status == "closed"])]
        self._fill(self.open_table, open_rows)
        self._fill(self.closed_table, closed_rows)
        self.open_label.setText(tr("paper.open_title", count=len(open_rows)))
        self.closed_label.setText(tr("paper.closed_title", count=len(closed_rows)))

    @staticmethod
    def _fill(table: Any, rows: Sequence[tuple]) -> None:
        table.setRowCount(0)
        for values in rows:
            row = table.rowCount()
            table.insertRow(row)
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(str(value)))

    def retranslate(self) -> None:
        self.refresh_button.setText(tr("paper.refresh"))
        self.resume_button.setText(tr("paper.resume"))
        self.open_button.setText(tr("paper.open"))
        self.close_button.setText(tr("paper.close"))
        self.ticker.setPlaceholderText(tr("paper.ticker"))
        self.open_table.setHorizontalHeaderLabels([tr(f"paper.col.{c}") for c in (
            "symbol", "opened", "qty", "entry", "stop", "target", "last", "pnl")])
        self.closed_table.setHorizontalHeaderLabels([tr(f"paper.col.{c}") for c in (
            "symbol", "opened", "closed", "qty", "entry", "exit", "why", "pnl")])
        self.note.setText(tr("paper.note"))
        self.resume_button.setVisible(False)
        if self.snapshot:
            self.show_snapshot(self.snapshot)
        else:
            self.open_label.setText(tr("paper.open_title", count=0))
            self.closed_label.setText(tr("paper.closed_title", count=0))
