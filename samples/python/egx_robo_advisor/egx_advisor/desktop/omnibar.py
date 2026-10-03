"""The Omnibar: type a ticker, press Enter, see everything about it on one page.

The bar sits in the window's header on every page (Ctrl+K focuses it). Enter
opens the Quick View and starts four things at once:

1. the analyst team's quick opinion (the dashboard's chat, so the committee,
   its budget and its buy gate apply, and its decision feeds the Today cards);
2. the latest news for the stock (analyst/tools.search_news);
3. the TradingView chart with the operator's indicators (right away, in the
   Quick View itself);
4. the stock's page in the Thndr X tab (the analyst's browse channel).

The three slow ones run through workers.run_async and report back on the UI
thread. Each search gets a number; a result from an older search is dropped,
so typing a second ticker quickly never shows the first one's answers.
"""

from __future__ import annotations

import html
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QStringListModel, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import i18n
from ..browse import ticker_of
from ..i18n import tr
from . import theme
from .workers import run_async

OPINION_QUESTION = ("رأي سريع في سهم {ticker}: شراء ولا احتفاظ ولا بيع؟ القرار والسبب "
                    "والوقف والهدف باختصار.")


class Omnibar(QLineEdit):
    """A ticker search box; emits `searched(ticker)` on Enter."""

    searched = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Omnibar")
        self.setClearButtonEnabled(True)
        self.setMinimumWidth(320)
        self._model = QStringListModel(self)
        completer = QCompleter(self._model, self)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        self.setCompleter(completer)
        self.returnPressed.connect(self._submit)
        self.retranslate()

    def bind_shortcut(self, window: QWidget) -> None:
        QShortcut(QKeySequence("Ctrl+K"), window, activated=self._focus)

    def _focus(self) -> None:
        self.setFocus(Qt.ShortcutFocusReason)
        self.selectAll()

    def retranslate(self) -> None:
        self.setPlaceholderText(tr("omni.placeholder"))

    def set_tickers(self, tickers: Iterable[str]) -> None:
        names = sorted({t for t in (ticker_of(s) for s in tickers) if t})
        self._model.setStringList(names)

    def _submit(self) -> None:
        ticker = ticker_of(self.text())
        if not ticker:
            self.setToolTip(tr("omni.bad_ticker", text=self.text()))
            theme.restyle(self)
            return
        self.setText(ticker)
        self.searched.emit(ticker)


# --------------------------------------------------------------------------- #
# The jobs, off the UI thread
# --------------------------------------------------------------------------- #


def fetch_news(services: Any, ticker: str) -> dict[str, Any]:
    with services.bus() as bus:
        return services.toolbox(bus).search_news(ticker, limit=8)


def fetch_opinion(services: Any, ticker: str) -> str:
    return services.ask_team(OPINION_QUESTION.format(ticker=ticker))


def open_in_thndr(services: Any, ticker: str) -> dict[str, Any]:
    client = services.browse()
    if client is None:
        return {"ok": False, "error": tr("today.no_browser")}
    return client.call("open_stock", symbol=ticker)


class QuickView(QWidget):
    """One stock at a glance: opinion and news beside its chart; Thndr X opened."""

    def __init__(self, services: Any, studies: Callable[[], Optional[list[str]]],
                 go_thndr: Callable[[], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.studies = studies
        self.request = 0
        self.ticker = ""

        self.title = QLabel()
        self.title.setObjectName("QuickTitle")
        self.thndr_status = QLabel()
        self.thndr_status.setProperty("pill", "muted")
        self.thndr_button = QPushButton()
        self.thndr_button.setProperty("variant", "ghost")
        self.thndr_button.clicked.connect(go_thndr)
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.thndr_status)
        top.addWidget(self.thndr_button)

        self.opinion_label = QLabel()
        self.opinion_label.setObjectName("SectionTitle")
        self.opinion = QTextBrowser()
        self.opinion.setOpenExternalLinks(True)
        self.news_label = QLabel()
        self.news_label.setObjectName("SectionTitle")
        self.news = QListWidget()
        self.news.setWordWrap(True)
        self.news.itemActivated.connect(self._open_link)
        left = QWidget()
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.opinion_label)
        column.addWidget(self.opinion, 3)
        column.addWidget(self.news_label)
        column.addWidget(self.news, 2)

        # Its own chart view (private profile), so the Chart tab keeps its symbol.
        self.profile = QWebEngineProfile(self)
        self.chart = QWebEngineView()
        self.chart.setPage(QWebEnginePage(self.profile, self.chart))
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.chart)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        self.retranslate()

    def retranslate(self) -> None:
        self.opinion_label.setText(tr("omni.opinion"))
        self.news_label.setText(tr("omni.news"))
        self.thndr_button.setText(tr("omni.go_thndr"))
        self.title.setText(tr("omni.title", ticker=self.ticker) if self.ticker
                           else tr("omni.empty"))

    # ------------------------------------------------------------------ orchestration

    def show_ticker(self, ticker: str) -> None:
        """Start all four at once. UI thread."""
        from .chart_tab import widget_html

        self.request += 1
        request, services = self.request, self.services
        self.ticker = ticker
        self.retranslate()
        self.opinion.setPlainText(tr("omni.asking"))
        self.news.clear()
        self.news.addItem(tr("omni.loading"))
        theme.restyle_pill(self.thndr_status, "muted")
        self.thndr_status.setText(tr("omni.thndr_opening"))

        # 3. the chart: no network wait on our side, the page loads itself.
        self.chart.setHtml(widget_html(ticker + ".CA", theme.current(), i18n.current(),
                                       self.studies()),
                           QUrl("https://egx-robo-advisor.invalid/quick"))
        # 1, 2, 4: in parallel, off the UI thread.
        run_async(lambda: fetch_opinion(services, ticker),
                  lambda r, e: self._opinion(request, r, e), owner=self)
        run_async(lambda: fetch_news(services, ticker),
                  lambda r, e: self._news(request, r, e), owner=self)
        run_async(lambda: open_in_thndr(services, ticker),
                  lambda r, e: self._thndr(request, r, e), owner=self)

    def _current(self, request: int) -> bool:
        return request == self.request

    def _opinion(self, request: int, text: Optional[str], error: Optional[BaseException]
                 ) -> None:
        if not self._current(request):
            return
        if error is not None:
            self.opinion.setPlainText(tr("omni.failed", error=f"{type(error).__name__}: "
                                                              f"{error}"))
            return
        body = html.escape(text or tr("omni.no_answer")).replace("\n", "<br>")
        self.opinion.setHtml(f'<div dir="auto" style="line-height:1.6">{body}</div>')

    def _news(self, request: int, result: Optional[dict[str, Any]],
              error: Optional[BaseException]) -> None:
        if not self._current(request):
            return
        self.news.clear()
        if error is not None or "error" in (result or {}):
            self.news.addItem(tr("omni.failed", error=error or (result or {}).get("error")))
            return
        items = (result or {}).get("items") or []
        if not items:
            self.news.addItem(tr("omni.no_news"))
        for n in items:
            item = QListWidgetItem(f"{n.get('date', '')}  {n.get('title', '')}\n"
                                   f"{n.get('source', '')}")
            item.setData(Qt.UserRole, n.get("url") or "")
            item.setToolTip(tr("omni.open_link"))
            self.news.addItem(item)

    def _thndr(self, request: int, result: Optional[dict[str, Any]],
               error: Optional[BaseException]) -> None:
        if not self._current(request):
            return
        ok = error is None and bool((result or {}).get("ok"))
        theme.restyle_pill(self.thndr_status, "ok" if ok else "warn")
        if ok:
            self.thndr_status.setText(tr("omni.thndr_opened"))
        else:
            why = error or (result or {}).get("refused") or (result or {}).get("error") \
                or (result or {}).get("reason") or "-"
            self.thndr_status.setText(tr("omni.thndr_failed", reason=str(why)[:120]))

    @staticmethod
    def _open_link(item: QListWidgetItem) -> None:
        url = str(item.data(Qt.UserRole) or "")
        if url.startswith(("https://", "http://")):
            # The system browser, not the Thndr X session.
            QDesktopServices.openUrl(QUrl(url))

    def release(self) -> None:
        page = self.chart.page()
        self.chart.setPage(QWebEnginePage(self.chart))
        page.deleteLater()
