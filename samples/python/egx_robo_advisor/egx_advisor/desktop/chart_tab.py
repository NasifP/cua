"""The Chart tab: TradingView's own chart widget for the names you hold or target.

TradingView publishes this widget for embedding in other sites and apps, so
this uses it as offered rather than scraping its pages. The chart is for you;
nothing on it reaches the bot. It runs in a private browser profile, apart from
the Thndr X session.
"""

from __future__ import annotations

import html as _html
import json
from typing import Any, Callable, Optional

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import chart_marks, i18n
from ..i18n import tr
from . import theme


def _placeholder_html(text: str) -> str:
    t = theme.tokens()
    return (f'<html><body style="margin:0;background:{t["chart_bg"]};color:{t["muted"]};'
            f'font:15px system-ui,sans-serif"><p dir="auto" style="padding:28px">'
            f"{_html.escape(text)}</p></body></html>")


def load_marks(symbol: str, *, history: Callable[..., Any], memory: Any = None,
               open_bus: Optional[Callable[[], Any]] = None, style: str = "swing"
               ) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], str]:
    """(bars, marks, price lines, legend) for the Signals view. Off the UI thread.

    Bars up to the previous session; the operator's indicators and past picks
    from memory; stop/targets and the team's view from the bus.
    """
    from datetime import date

    from .. import indicators, levels
    from ..browse import ticker_of

    ticker = ticker_of(symbol)
    if not ticker:
        return [], [], [], ""
    yahoo = ticker + ".CA"
    today = date.today()
    rows = sorted((r for r in (history([yahoo], 420).get(yahoo) or ()) if r.day < today),
                  key=lambda r: r.day)
    choices, picks = indicators.default_set(), []
    if memory is not None:
        saved = memory.ui_get(memory.INDICATORS_KEY)
        choices = [c for c in indicators.load_set(saved) if c.on] if saved else choices
        picks = memory.picks()
    view, lv, risk_off = None, None, False
    if open_bus is not None:
        with open_bus() as bus:
            view = ((bus.get("team_views") or {}).get("payload") or {}).get(ticker)
            snap = (bus.get("levels") or {}).get("payload") or {}
            lv = (snap.get("levels") or {}).get(yahoo)
            risk_off = bool((snap.get("macro") or {}).get("MACRO_RISK_OFF"))
    if lv is None and rows:
        computed = levels.compute(yahoo, rows, float(rows[-1].close), style, None, risk_off)
        lv = computed.to_json() if computed else None
    marks, lines = chart_marks.build(ticker, rows, choices=choices, picks=picks, levels=lv,
                                     view=view)
    legend = tr("chart.legend")
    if view and view.get("decision"):
        legend += " · " + tr("chart.legend_team", decision=view["decision"],
                             day=view.get("day", ""))
    return rows, marks, lines, legend

WIDGET_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>html,body,#tv{{margin:0;height:100%;width:100%;background:{bg};color:{muted};
font:15px system-ui,sans-serif}} #tv p{{padding:28px;line-height:1.7}}</style></head>
<body><div id="tv"></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
if (!window.TradingView) {{
  var p = document.createElement("p");
  p.dir = "{dir}";
  p.textContent = {failed};
  document.getElementById("tv").appendChild(p);
}} else new TradingView.widget({{
  container_id: "tv", autosize: true, symbol: {symbol}, interval: "D",
  timezone: "Africa/Cairo", theme: "{theme}", style: "1", locale: "{locale}",
  allow_symbol_change: true, studies: {studies}
}});
</script></body></html>"""


def tradingview_symbol(symbol: str) -> str:
    """COMI.CA -> EGX:COMI. TradingView lists EGX names under the EGX prefix.

    A symbol that already names its exchange (TVC:GOLD, EGX:COMI) is kept.
    """
    symbol = symbol.strip().upper()
    if ":" in symbol:
        return symbol
    return "EGX:" + symbol.removesuffix(".CA")


def _js_string(text: str) -> str:
    # json.dumps quotes and escapes; escaping "<" as well means a typed
    # "</script>" cannot end the script block early.
    return json.dumps(text).replace("<", "\\u003c")


#: The chart's indicators until the operator saves their own (Training tab).
DEFAULT_STUDIES = ("MASimple@tv-basicstudies", "RSI@tv-basicstudies")


def _js_string_list(items: list[str]) -> str:
    return "[" + ", ".join(_js_string(i) for i in items) + "]"


def widget_html(symbol: str, theme_name: str = "dark", lang: str = "ar",
                studies: Optional[list[str]] = None) -> str:
    name = theme_name if theme_name in theme.THEMES else theme.DEFAULT
    t = theme.tokens(name)
    studies = [s for s in (DEFAULT_STUDIES if studies is None else studies)
               if isinstance(s, str) and s.endswith("@tv-basicstudies")]
    return WIDGET_HTML.format(
        studies=_js_string_list(studies),
        symbol=_js_string(tradingview_symbol(symbol)), theme=name, bg=t["chart_bg"],
        muted=t["muted"], locale="ar_AE" if lang == "ar" else "en",
        dir="rtl" if lang == "ar" else "ltr", failed=_js_string(tr("chart.failed", lang=lang)),
    )


def chart_symbols() -> list[str]:
    """Holdings from the last portfolio read first, then the policy universe."""
    from ..bus import StateBus
    from ..paths import bus_path
    from ..strategy.policy import AllocationPolicy

    held: list[str] = []
    try:
        with StateBus(bus_path()) as bus:
            payload = (bus.get("portfolio") or {}).get("payload") or {}
            held = [p["symbol"] for p in payload.get("positions", []) if p.get("symbol")]
    except Exception:  # noqa: BLE001 - no bus yet: just the universe
        pass
    universe = [i.symbol for i in AllocationPolicy().universe]
    return held + [s for s in universe if s not in held]


#: marks_loader(symbol) -> (bars, marks, price lines, legend); runs off the UI thread.
MarksLoader = Callable[[str], tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], str]]


class ChartTab(QWidget):
    def __init__(self, parent: Optional[QWidget] = None,
                 studies: Optional[list[str]] = None,
                 marks_loader: Optional[MarksLoader] = None) -> None:
        super().__init__(parent)
        #: TradingView studies drawn on the chart (the operator's indicators).
        self.studies = studies
        #: Builds the Signals view (chart_marks.py); None hides that view.
        self.marks_loader = marks_loader
        self.mode = "tv"
        self._request = 0
        self.tv_button = QPushButton()
        self.signals_button = QPushButton()
        for button, mode in ((self.tv_button, "tv"), (self.signals_button, "signals")):
            button.setCheckable(True)
            button.setProperty("variant", "ghost")
            button.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
        self.tv_button.setChecked(True)
        self.signals_button.setVisible(marks_loader is not None)
        self.symbol = QComboBox()
        self.symbol.setEditable(True)
        # Not currentTextChanged: that fires on every keystroke of a typed symbol.
        self.symbol.activated.connect(lambda _i: self._show(self.symbol.currentText()))
        self.symbol.lineEdit().returnPressed.connect(
            lambda: self._show(self.symbol.currentText()))
        self.symbol.setMinimumWidth(260)
        self.refresh = QPushButton()
        self.refresh.clicked.connect(self.reload_symbols)
        self.symbol_label = QLabel()
        bar = QHBoxLayout()
        bar.addWidget(self.symbol_label)
        bar.addWidget(self.symbol)
        bar.addWidget(self.refresh)
        bar.addStretch(1)
        bar.addWidget(self.tv_button)
        bar.addWidget(self.signals_button)

        # Off-the-record profile: no cookies shared with the Thndr X tab.
        self.profile = QWebEngineProfile(self)
        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(self.profile, self.view))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)
        self._shown = ""
        self.retranslate()
        self.reload_symbols()

    def retranslate(self) -> None:
        self.symbol_label.setText(tr("chart.symbol"))
        self.symbol.lineEdit().setPlaceholderText(tr("chart.placeholder"))
        self.symbol.setToolTip(tr("chart.symbol_tip"))
        self.refresh.setText(tr("chart.refresh"))
        self.refresh.setToolTip(tr("chart.refresh_tip"))
        self.tv_button.setText(tr("chart.mode.tv"))
        self.signals_button.setText(tr("chart.mode.signals"))
        self.signals_button.setToolTip(tr("chart.mode.signals_tip"))
        if self._shown:
            self._show(self._shown)

    def reload_symbols(self) -> None:
        current = self.symbol.currentText()
        self.symbol.blockSignals(True)
        self.symbol.clear()
        self.symbol.addItems(chart_symbols())
        if current:
            self.symbol.setCurrentText(current)
        self.symbol.blockSignals(False)
        if self.isVisible():
            self._show(self.symbol.currentText())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Nothing is fetched from TradingView until the tab is first opened.
        super().showEvent(event)
        if not self._shown:
            self._show(self.symbol.currentText())

    def show_symbol(self, symbol: str) -> None:
        """Show `symbol` (the Omnibar and the cards call this)."""
        self.symbol.setCurrentText(symbol)
        self._show(symbol)

    def set_mode(self, mode: str) -> None:
        """"tv": TradingView's widget; "signals": our bars with buy/sell marks."""
        self.mode = mode if mode == "tv" or self.marks_loader is not None else "tv"
        self.tv_button.setChecked(self.mode == "tv")
        self.signals_button.setChecked(self.mode == "signals")
        if self._shown:
            self._show(self._shown)

    def _show(self, symbol: str) -> None:
        symbol = symbol.strip()
        if not symbol:
            return
        self._shown = symbol
        if self.mode == "signals" and self.marks_loader is not None:
            self._show_signals(symbol)
            return
        self.view.setHtml(widget_html(symbol, theme.current(), i18n.current(), self.studies),
                          QUrl("https://egx-robo-advisor.invalid/chart"))

    def _show_signals(self, symbol: str) -> None:
        """Bars and marks built off the UI thread; the page is set on it."""
        from .workers import run_async

        self._request += 1
        request, loader = self._request, self.marks_loader
        self.view.setHtml(_placeholder_html(tr("chart.signals_loading")))

        def done(result: Any, error: Optional[BaseException]) -> None:
            if request != self._request or self.mode != "signals":
                return  # a newer symbol or the other view: drop this one
            if error is not None or not result or not result[0]:
                why = f"{type(error).__name__}: {error}" if error else tr("chart.no_bars")
                self.view.setHtml(_placeholder_html(why))
                return
            bars, marks, lines, legend = result
            self.view.setHtml(chart_marks.page_html(
                bars, marks, lines, dark=theme.current() == "dark", legend=legend,
                failed=tr("chart.failed")), QUrl("https://egx-robo-advisor.invalid/signals"))

        run_async(lambda: loader(symbol), done, owner=self)

    def update_marks(self, marks: list[dict[str, Any]], lines: list[dict[str, Any]]) -> None:
        """Replace the marks on the open Signals page without reloading it (UI thread)."""
        if self.mode == "signals":
            self.view.page().runJavaScript(chart_marks.set_marks_script(marks, lines))

    def set_studies(self, studies: list[str]) -> None:
        """The operator saved their indicators: draw those."""
        self.studies = list(studies)
        if self._shown:
            self._show(self._shown)

    def set_theme(self, _name: str) -> None:
        if self._shown:
            self._show(self._shown)

    def release(self) -> None:
        """Drop the page before its profile; the main window calls this on close."""
        page = self.view.page()
        self.view.setPage(QWebEnginePage(self.view))
        page.deleteLater()
