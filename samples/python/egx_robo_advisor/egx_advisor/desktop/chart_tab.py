"""The Chart tab: TradingView's own chart widget for the names you hold or target.

TradingView publishes this widget for embedding in other sites and apps, so
this uses it as offered rather than scraping its pages. The chart is for you;
nothing on it reaches the bot. It runs in a private browser profile, apart from
the Thndr X session.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import i18n
from ..i18n import tr
from . import theme

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
  allow_symbol_change: true, studies: ["MASimple@tv-basicstudies", "RSI@tv-basicstudies"]
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


def widget_html(symbol: str, theme_name: str = "dark", lang: str = "ar") -> str:
    name = theme_name if theme_name in theme.THEMES else theme.DEFAULT
    t = theme.tokens(name)
    return WIDGET_HTML.format(
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


class ChartTab(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
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

    def _show(self, symbol: str) -> None:
        symbol = symbol.strip()
        if symbol:
            self._shown = symbol
            self.view.setHtml(widget_html(symbol, theme.current(), i18n.current()),
                              QUrl("https://egx-robo-advisor.invalid/chart"))

    def set_theme(self, _name: str) -> None:
        if self._shown:
            self._show(self._shown)

    def release(self) -> None:
        """Drop the page before its profile; the main window calls this on close."""
        page = self.view.page()
        self.view.setPage(QWebEnginePage(self.view))
        page.deleteLater()
