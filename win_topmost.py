"""Force a Qt widget to stay above other windows without stealing focus."""

from __future__ import annotations

import sys
from typing import Any


def force_topmost(widget: Any, *, activate: bool = False) -> bool:
    """Re-assert topmost z-order.

    On Windows uses HWND_TOPMOST with SWP_NOACTIVATE.
    On macOS / Linux: do **not** call raise_() by default — that activates the
    whole Qt application and pulls the main hub in front of other apps.
    ``WindowStaysOnTopHint`` already keeps the float above; periodic raise was
    the focus-stealing bug.
    """
    if widget is None:
        return False
    try:
        if hasattr(widget, "isVisible") and not widget.isVisible():
            return False
    except Exception:
        return False

    if sys.platform != "win32":
        if activate:
            try:
                widget.raise_()
            except Exception:
                pass
        return True

    if activate:
        try:
            widget.raise_()
        except Exception:
            pass

    try:
        import win32con  # type: ignore
        import win32gui  # type: ignore

        hwnd = int(widget.winId())
        if not hwnd:
            return False
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_SHOWWINDOW
            | win32con.SWP_NOACTIVATE,
        )
        return True
    except Exception:
        return False
