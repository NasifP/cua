"""Which Qt files the packaged app leaves out. Used by egx_robo_advisor.spec.

Qt WebEngine pulls in PyInstaller's QML hooks, which collect every QML module
Qt ships (3D, charts, multimedia, ...). The widgets app loads none of them:
WebEngine links Core, Gui, Widgets, Network, OpenGL, DBus, Positioning,
PrintSupport, Qml, Quick, QuickWidgets and WebChannel, and the icons need Svg.
Dropping the rest removes about a fifth of the download. Chromium's interface
translations are kept for Arabic and English only.

The build's self-check (egx_app.py --role selfcheck) imports Qt WebEngine from
the result, so a file removed here that was needed fails the build.
"""

from __future__ import annotations

import re

QT_UNUSED = (
    "3D", "Quick3D", "Charts", "DataVisualization", "Graphs", "Multimedia", "SpatialAudio",
    "Location", "Sensors", "TextToSpeech", "WaylandCompositor", "VirtualKeyboard", "Scxml",
    "StateMachine", "RemoteObjects", "Pdf", "QuickControls2", "QuickDialogs2", "QuickTemplates2",
    "QuickParticles", "QuickShapes", "QuickTimeline", "QuickVectorImage", "QuickEffects",
    "QuickLayouts", "ShaderTools", "Bluetooth", "Nfc", "SerialPort", "SerialBus", "WebView",
    "HttpServer", "Protobuf", "Grpc", "Test", "5Compat", "Designer", "UiTools", "Help", "Sql",
    "Labs",
)
UNUSED_QT = re.compile(r"^(?:lib)?Qt6?(?:" + "|".join(QT_UNUSED) + ")", re.IGNORECASE)
KEPT_LOCALES = ("en-US.pak", "ar.pak")


def keep(dest: str) -> bool:
    """Whether a file at `dest` (its path inside the bundle) is packaged."""
    path = dest.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if "PySide6/Qt/qml/" in path + "/" or "PySide6/qml/" in path + "/":
        return False
    if path.startswith("PySide6") and UNUSED_QT.match(name):
        return False
    if "/translations/" in path:
        if name.endswith(".pak"):
            return name in KEPT_LOCALES
        if name.endswith(".qm"):
            return re.search(r"_(ar|en)\.qm$", name) is not None
    return True
