"""The Today tab: the day's actions as cards, each with its own "Prepare order".

Cards come from daily.build (the analyst team's decisions, the bot's plan,
stops and targets). A buy card pre-fills a quantity (the plan's, else what
the risk manager's cap, cash and liquidity allow) and a limit price, and its
button runs the analyst's own prepare_buy: cash, price near the market, news
brake, HALT and liquidity are all checked there, the ticket panel is filled
and the stock opens in Thndr X. The operator still presses Buy.

A sell or trim card opens the stock in Thndr X: the app prepares no sell
tickets.

Everything slow runs through workers.run_async; widgets change only on the UI
thread.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import daily
from ..i18n import tr
from . import theme
from .workers import run_async

REFRESH_MS = 60_000
COLUMNS = 3
_PILL = {"buy": "ok", "add": "ok", "hold": "muted", "trim": "warn", "sell": "bad"}


def load_cards(services: Any) -> tuple[list[daily.Card], dict[str, int], Optional[float]]:
    """Off the UI thread: the cards, a suggested quantity per buy card, and cash."""
    from ..clock import CAIRO

    with services.bus() as bus:
        snapshots = bus.all_snapshots()
        halted = bus.control_state().halted
        cards = daily.build(snapshots, datetime.now(CAIRO).date(), halted=halted)
        cash_raw = ((snapshots.get("portfolio") or {}).get("payload") or {}).get("cash_egp")
        try:
            cash = float(cash_raw) if cash_raw not in (None, "") else None
        except (TypeError, ValueError):
            cash = None
        box = services.toolbox(bus)
        quantities = {}
        for card in cards:
            if not card.is_buy:
                continue
            try:
                liquidity = box.liquidity(card.ticker).max_shares
            except Exception:  # noqa: BLE001 - no volume: the cap is 0 anyway
                liquidity = 0
            quantities[card.ticker] = daily.suggested_quantity(card, cash, liquidity)
    return cards, quantities, cash


def prepare(services: Any, card: daily.Card, quantity: int, price: float) -> dict[str, Any]:
    """Off the UI thread: the analyst's prepare_buy, after the team's cap."""
    if card.max_egp and quantity * price > card.max_egp:
        return {"prepared": False, "refused": tr(
            "today.capped", cap=f"{card.max_egp:,.0f}", value=f"{quantity * price:,.0f}")}
    with services.bus() as bus:
        return services.toolbox(bus).prepare_buy(
            card.ticker, quantity, price, reason=f"{card.source}: {card.reason}"[:300])


def open_stock(services: Any, ticker: str) -> dict[str, Any]:
    client = services.browse()
    if client is None:
        return {"ok": False, "error": tr("today.no_browser")}
    return client.call("open_stock", symbol=ticker)


class CardWidget(QFrame):
    """One action: ticker, decision, reason, and the button."""

    def __init__(self, card: daily.Card, quantity: int, services: Any,
                 on_opened: Callable[[], None], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.card = card
        self.services = services
        self.on_opened = on_opened
        self.setObjectName("ActionCard")

        ticker = QLabel(card.ticker)
        ticker.setObjectName("CardTicker")
        decision = QLabel(tr(f"today.decision.{card.decision}"))
        decision.setProperty("pill", _PILL.get(card.decision, "muted"))
        source = QLabel(tr(f"today.source.{card.source}"))
        source.setObjectName("CardSource")
        top = QHBoxLayout()
        top.addWidget(ticker)
        top.addStretch(1)
        top.addWidget(source)
        top.addWidget(decision)

        reason = QLabel(card.reason)
        reason.setObjectName("CardReason")
        reason.setWordWrap(True)
        reason.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.button = QPushButton()
        self.button.setProperty("variant", "primary")
        self.button.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addLayout(top)
        layout.addWidget(reason, 1)

        self.quantity: Optional[QSpinBox] = None
        self.price: Optional[QDoubleSpinBox] = None
        if card.is_buy:
            self.quantity = QSpinBox()
            self.quantity.setRange(0, 1_000_000)
            self.quantity.setValue(quantity)
            self.quantity.setSuffix(" " + tr("today.shares"))
            self.price = QDoubleSpinBox()
            self.price.setDecimals(3)
            self.price.setRange(0, 1_000_000)
            self.price.setValue(card.limit_price or 0)
            self.price.setSuffix(" " + tr("today.egp"))
            order = QHBoxLayout()
            order.addWidget(self.quantity, 1)
            order.addWidget(self.price, 1)
            layout.addLayout(order)
            self.button.setText(tr("today.prepare"))
            self.button.setEnabled(card.can_prepare)
            if not card.can_prepare:
                theme.say(self.status, tr(f"today.blocked.{card.blocked}"), "info")
            self.button.clicked.connect(self._prepare)
        else:
            self.button.setText(tr("today.open"))
            self.button.clicked.connect(self._open)
        layout.addWidget(self.button)
        layout.addWidget(self.status)

    def _busy(self, text: str) -> None:
        self.button.setEnabled(False)
        theme.say(self.status, text, "info")

    def _prepare(self) -> None:
        quantity = self.quantity.value() if self.quantity else 0
        price = self.price.value() if self.price else 0.0
        if quantity <= 0 or price <= 0:
            theme.say(self.status, tr("today.need_numbers"), "bad")
            return
        self._busy(tr("today.preparing"))
        card, services = self.card, self.services
        run_async(lambda: prepare(services, card, quantity, price), self._prepared)

    def _prepared(self, result: Optional[dict[str, Any]], error: Optional[BaseException]
                  ) -> None:
        self.button.setEnabled(self.card.can_prepare)
        if error is not None:
            theme.say(self.status, f"{type(error).__name__}: {error}", "bad")
            return
        if not (result or {}).get("prepared"):
            theme.say(self.status, str((result or {}).get("refused") or result), "bad")
            return
        theme.say(self.status, tr("today.prepared"), "ok")
        self.on_opened()

    def _open(self) -> None:
        self._busy(tr("today.opening"))
        services, ticker = self.services, self.card.ticker
        run_async(lambda: open_stock(services, ticker), self._opened)

    def _opened(self, result: Optional[dict[str, Any]], error: Optional[BaseException]
                ) -> None:
        self.button.setEnabled(True)
        if error is not None or not (result or {}).get("ok"):
            why = error or (result or {}).get("refused") or (result or {}).get("error") \
                or (result or {}).get("reason")
            theme.say(self.status, str(why), "bad")
            return
        theme.say(self.status, tr("today.opened"), "ok")
        self.on_opened()


class TodayTab(QWidget):
    """Cards for the day, refreshed every minute while the tab is shown."""

    def __init__(self, services: Any, on_opened: Callable[[], None] = lambda: None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.services = services
        self.on_opened = on_opened
        self.summary = QLabel()
        self.summary.setObjectName("PageSubtitle")
        self.refresh_button = QPushButton()
        self.refresh_button.clicked.connect(self.refresh)
        bar = QHBoxLayout()
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.refresh_button)

        self.grid = QGridLayout()
        self.grid.setSpacing(14)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(self.grid)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(holder)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addLayout(bar)
        layout.addWidget(scroll, 1)

        self._loading = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(REFRESH_MS)
        self.cards: list[CardWidget] = []
        self.retranslate()

    def retranslate(self) -> None:
        self.refresh_button.setText(tr("today.refresh"))
        if not self.cards:
            self.summary.setText(tr("today.loading"))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh()

    def _tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        services = self.services
        run_async(lambda: load_cards(services), self._loaded)

    def _loaded(self, result: Any, error: Optional[BaseException]) -> None:
        self._loading = False
        if error is not None:
            self.summary.setText(tr("today.failed", error=f"{type(error).__name__}: {error}"))
            return
        cards, quantities, _cash = result
        self.show_cards(cards, quantities)

    def show_cards(self, cards: list[daily.Card], quantities: dict[str, int]) -> None:
        """Replace the grid (UI thread only)."""
        for widget in self.cards:
            self.grid.removeWidget(widget)
            widget.deleteLater()
        self.cards = []
        for i, card in enumerate(cards):
            widget = CardWidget(card, quantities.get(card.ticker, 0), self.services,
                                self.on_opened)
            self.grid.addWidget(widget, i // COLUMNS, i % COLUMNS)
            self.cards.append(widget)
        self.summary.setText(tr("today.summary", count=len(cards),
                                time=datetime.now().strftime("%H:%M"))
                             if cards else tr("today.empty"))
