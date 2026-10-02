"""Cross-platform Qt window flags for overlays / floating chrome."""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt


def interactive_overlay_flags() -> Qt.WindowType:
    """Frameless always-on-top flags that stay interactive on each OS.

    On macOS, ``Qt.Tool`` windows are hidden or stop receiving clicks when the
    app is not active — that broke screenshot overlays and the recorder
    toolbar (could not expand / stop). Use a normal ``Window`` there (and on
    Linux). Windows keeps ``Tool`` so the bar stays out of the taskbar.
    """
    flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
    if sys.platform.startswith("win"):
        flags |= Qt.WindowType.Tool
    else:
        flags |= Qt.WindowType.Window
    return flags
