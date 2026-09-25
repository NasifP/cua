"""Colours, the application stylesheet and icons for the desktop app.

The colour tokens are the dashboard's own (dashboard/templates/index.html), so
the window around it and the page inside it read as one app. Two themes, dark
by default; the choice is kept in .env as EGX_THEME.

Widgets pick a look with a property instead of their own stylesheet:

    button.setProperty("variant", "primary")   # or "danger", "ghost"
    label.setProperty("pill", "ok")            # or "bad", "warn", "muted"
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#020618", "surface": "#0f172a", "raised": "#162033", "border": "#1e2939",
        "text": "#e2e8f0", "muted": "#94a3b8", "faint": "#64748b",
        "accent": "#38bdf8", "on_accent": "#04121c", "accent_soft": "#0c2a3d",
        "ok": "#34d399", "ok_bg": "#022c22", "warn": "#fbbf24", "warn_bg": "#2a1c05",
        "bad": "#f87171", "bad_bg": "#2a0a0a", "danger": "#dc2626", "input": "#020618",
        "chart_bg": "#131722",
    },
    "light": {
        "bg": "#f1f5f9", "surface": "#ffffff", "raised": "#f8fafc", "border": "#e2e8f0",
        "text": "#0f172a", "muted": "#64748b", "faint": "#94a3b8",
        "accent": "#0284c7", "on_accent": "#ffffff", "accent_soft": "#e0f2fe",
        "ok": "#059669", "ok_bg": "#ecfdf5", "warn": "#d97706", "warn_bg": "#fffbeb",
        "bad": "#dc2626", "bad_bg": "#fef2f2", "danger": "#dc2626", "input": "#ffffff",
        "chart_bg": "#ffffff",
    },
}
DEFAULT = "dark"

_current = DEFAULT


def name_from_env(env: Optional[dict[str, str]] = None) -> str:
    value = ((env if env is not None else os.environ).get("EGX_THEME") or "").strip().lower()
    return value if value in THEMES else DEFAULT


def current() -> str:
    return _current


def tokens(name: Optional[str] = None) -> dict[str, str]:
    return THEMES[name or _current]


def stylesheet(name: str) -> str:
    t = THEMES[name]
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t['text']}; font-size: 10pt; }}
QMainWindow, QWidget#Page, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{
    background: {t['bg']};
}}
QScrollArea {{ border: none; }}
QToolTip {{
    background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']};
    padding: 6px 8px; border-radius: 6px;
}}

/* ---- sidebar ---- */
QWidget#Sidebar {{ background: {t['surface']}; border-left: 1px solid {t['border']}; }}
QLabel#BrandMark {{
    background: {t['accent']}; color: {t['on_accent']}; border-radius: 10px;
    font-weight: 800; font-size: 10pt;
}}
QLabel#BrandName {{ font-size: 12pt; font-weight: 700; }}
QLabel#BrandSub, QLabel#Hint {{ color: {t['faint']}; font-size: 8.5pt; }}
QPushButton#NavButton {{
    text-align: right; padding: 10px 12px; border: none; border-radius: 10px;
    background: transparent; color: {t['muted']}; font-size: 10.5pt; font-weight: 600;
}}
QPushButton#NavButton:hover {{ background: {t['raised']}; color: {t['text']}; }}
QPushButton#NavButton:checked {{ background: {t['accent_soft']}; color: {t['accent']}; }}

/* ---- page header ---- */
QWidget#Header {{ background: {t['bg']}; border-bottom: 1px solid {t['border']}; }}
QLabel#PageTitle {{ font-size: 17pt; font-weight: 700; }}
QLabel#PageSubtitle {{ color: {t['muted']}; font-size: 9.5pt; }}
QLabel#SpendText {{ color: {t['muted']}; font-size: 9pt; }}
QLabel#SpendValue {{ color: {t['text']}; font-size: 9pt; font-weight: 700; }}
QProgressBar#Spend {{
    background: {t['border']}; border: none; border-radius: 3px; max-height: 6px;
    min-height: 6px;
}}
QProgressBar#Spend::chunk {{ background: {t['accent']}; border-radius: 3px; }}
QProgressBar#Spend[level="warn"]::chunk {{ background: {t['warn']}; }}
QProgressBar#Spend[level="bad"]::chunk {{ background: {t['bad']}; }}

/* ---- pills and messages ---- */
QLabel[pill] {{ border-radius: 11px; padding: 3px 11px; font-weight: 700; font-size: 9pt; }}
QLabel[pill="ok"] {{ background: {t['ok_bg']}; color: {t['ok']}; }}
QLabel[pill="bad"] {{ background: {t['bad_bg']}; color: {t['bad']}; }}
QLabel[pill="warn"] {{ background: {t['warn_bg']}; color: {t['warn']}; }}
QLabel[pill="muted"] {{ background: {t['raised']}; color: {t['muted']}; font-weight: 600; }}
QLabel[message] {{ border-radius: 8px; padding: 8px 12px; }}
QLabel[message=""] {{ padding: 0; }}
QLabel[message="ok"] {{ background: {t['ok_bg']}; color: {t['ok']}; }}
QLabel[message="bad"] {{ background: {t['bad_bg']}; color: {t['bad']}; }}
QLabel[message="info"] {{ background: {t['raised']}; color: {t['muted']}; }}
QLabel[muted="true"] {{ color: {t['muted']}; }}

/* ---- buttons ---- */
QPushButton {{
    background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']};
    border-radius: 8px; padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {t['accent']}; }}
QPushButton:pressed {{ background: {t['raised']}; }}
QPushButton:disabled {{
    color: {t['faint']}; border-color: {t['border']}; background: {t['raised']};
}}
QPushButton[variant="primary"] {{
    background: {t['accent']}; color: {t['on_accent']}; border: none;
}}
QPushButton[variant="primary"]:hover {{ background: {t['accent']}; border: none; }}
QPushButton[variant="primary"]:disabled {{ background: {t['raised']}; color: {t['faint']}; }}
QPushButton[variant="danger"] {{
    background: {t['danger']}; color: #ffffff; border: none; font-weight: 800;
    padding: 11px 14px; font-size: 10.5pt;
}}
QPushButton[variant="danger"]:hover {{ background: #b91c1c; }}
QPushButton[variant="ghost"] {{ background: transparent; border: none; color: {t['muted']}; }}
QPushButton[variant="ghost"]:hover {{ color: {t['text']}; background: {t['raised']}; }}

/* ---- cards ---- */
QGroupBox {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 12px;
    margin-top: 26px; padding: 12px 14px 14px 14px; font-weight: 700;
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top right; right: 4px; top: 2px;
    padding: 0 2px; color: {t['text']}; font-size: 10.5pt;
}}
QGroupBox QLabel {{ font-weight: 400; }}

/* ---- inputs ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {t['input']}; border: 1px solid {t['border']}; border-radius: 8px;
    padding: 6px 9px; selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus,
QPlainTextEdit:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled {{ color: {t['faint']}; }}
QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {t['surface']}; border: 1px solid {t['border']}; border-radius: 8px;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button,
QDoubleSpinBox::down-button {{ border: none; width: 16px; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px; border: 1px solid {t['faint']};
    background: {t['input']};
}}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}

/* ---- tables and lists ---- */
QTableWidget, QListWidget {{
    background: {t['surface']}; alternate-background-color: {t['raised']};
    border: 1px solid {t['border']}; border-radius: 10px; gridline-color: transparent;
    selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
}}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; }}
QTableWidget::item {{ padding: 4px 8px; }}
QHeaderView::section {{
    background: {t['surface']}; color: {t['muted']}; border: none;
    border-bottom: 1px solid {t['border']}; padding: 7px 8px; font-weight: 700;
    font-size: 9pt;
}}
QTableCornerButton::section {{ background: {t['surface']}; border: none; }}

/* ---- scrollbars ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{
    background: {t['border']}; border-radius: 4px; min-height: 24px; min-width: 24px;
}}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply(app: QApplication, name: str) -> None:
    """Style the whole application. Safe to call again to switch themes."""
    global _current
    _current = name if name in THEMES else DEFAULT
    t = THEMES[_current]
    if os.name == "nt":
        app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")  # the native Windows style ignores parts of a stylesheet
    # Arabic first: the sidebar sits on the right and forms read right to left.
    # English text inside a widget still runs left to right.
    app.setLayoutDirection(Qt.RightToLeft)
    palette = QPalette()
    for role, key in (
        (QPalette.Window, "bg"), (QPalette.Base, "input"), (QPalette.AlternateBase, "raised"),
        (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.Button, "surface"),
        (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
        (QPalette.HighlightedText, "on_accent"), (QPalette.ToolTipBase, "surface"),
        (QPalette.ToolTipText, "text"), (QPalette.PlaceholderText, "faint"),
        (QPalette.Link, "accent"),
    ):
        palette.setColor(role, QColor(t[key]))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(_current))


def restyle(widget: QWidget) -> None:
    """Re-read the stylesheet after a property such as `pill` changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def restyle_pill(label: QLabel, kind: str) -> None:
    label.setProperty("pill", kind)
    restyle(label)


def say(label: QLabel, text: str, kind: str = "ok") -> None:
    """Show a message in a label styled as a banner: kind is ok, bad or info."""
    label.setText(text)
    label.setProperty("message", kind if text else "")
    restyle(label)


def set_variant(*buttons: QWidget, variant: str) -> None:
    for button in buttons:
        button.setProperty("variant", variant)
        restyle(button)


# --------------------------------------------------------------------------- #
# Icons: small line drawings, tinted to the theme at runtime
# --------------------------------------------------------------------------- #

_ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" '
                 'width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" '
                 'rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "browser": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.5 2.7 '
               '3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/>',
    "chart": '<path d="M7 3v4M7 17v4M17 3v2M17 15v6"/><rect x="4.5" y="7" width="5" '
             'height="10" rx="1"/><rect x="14.5" y="5" width="5" height="10" rx="1"/>',
    "lab": '<path d="M9 3h6M10 3v6.5L4.8 18.2A1.8 1.8 0 0 0 6.4 21h11.2a1.8 1.8 0 0 0 '
           '1.6-2.8L14 9.5V3"/><path d="M7.5 15h9"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4'
                'M16 3v4"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" '
                'r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "power": '<path d="M12 3v9"/><path d="M6.3 6.3a8 8 0 1 0 11.4 0"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 '
           '17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    "moon": '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
}


def _pixmap(name: str, color: str, size: int = 40) -> QPixmap:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round">{_ICONS[name]}</svg>'
    )
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def icon(name: str, color: Optional[str] = None, checked_color: Optional[str] = None) -> QIcon:
    """An icon in `color` (the muted text colour by default); `checked_color` when on."""
    t = tokens()
    result = QIcon()
    result.addPixmap(_pixmap(name, color or t["muted"]), QIcon.Normal, QIcon.Off)
    result.addPixmap(_pixmap(name, checked_color or color or t["accent"]), QIcon.Normal, QIcon.On)
    return result
