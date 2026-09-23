"""Flameshot-style region screenshot + in-place annotation editor.

Tools (aligned with https://flameshot.org/ feature set):
  pen, marker/highlighter, arrow, rectangle, ellipse, text, number counter,
  pixelate, blur, solid fill box; undo/redo; copy; save; upload; pin.

Capture uses virtual desktop (multi-monitor). Editing is on a fullscreen overlay.
"""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

try:
    import mss
except ImportError:
    mss = None  # type: ignore

try:
    from PIL import Image, ImageFilter
except ImportError:
    Image = None  # type: ignore
    ImageFilter = None  # type: ignore


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def virtual_desktop_geometry() -> QRect:
    geo = QRect()
    for screen in QGuiApplication.screens():
        geo = geo.united(screen.geometry())
    if geo.isNull() or geo.width() <= 0:
        screen = QGuiApplication.primaryScreen()
        if screen:
            return screen.geometry()
        return QRect(0, 0, 1920, 1080)
    return geo


def capture_virtual_desktop() -> tuple[QPixmap, QRect]:
    """Grab entire virtual desktop. Returns (pixmap, geometry in global coords)."""
    geo = virtual_desktop_geometry()
    if mss is not None:
        with mss.mss() as sct:
            mon = sct.monitors[0]  # all monitors
            shot = sct.grab(mon)
            # BGRA -> RGBA
            arr = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4).copy()
            # mss is BGRA
            rgba = arr[:, :, [2, 1, 0, 3]].copy()
            img = QImage(rgba.data, shot.width, shot.height, shot.width * 4, QImage.Format.Format_RGBA8888).copy()
            left, top = mon["left"], mon["top"]
            return QPixmap.fromImage(img), QRect(left, top, shot.width, shot.height)
    # Fallback: Qt primary / stitched screens
    screens = QGuiApplication.screens()
    if not screens:
        raise RuntimeError("没有可用的显示器")
    # stitch
    geo = virtual_desktop_geometry()
    out = QPixmap(geo.width(), geo.height())
    out.fill(Qt.GlobalColor.black)
    painter = QPainter(out)
    for screen in screens:
        g = screen.geometry()
        pm = screen.grabWindow(0)
        painter.drawPixmap(g.x() - geo.x(), g.y() - geo.y(), pm)
    painter.end()
    return out, geo


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

@dataclass
class Stroke:
    kind: str  # pen|marker|arrow|rect|ellipse|text|number|emoji|pixelate|blur|fill
    points: list[QPointF] = field(default_factory=list)
    color: QColor = field(default_factory=lambda: QColor("#ff0000"))
    width: float = 6.0
    text: str = ""
    number: int = 0
    # text extras: readable labels on busy screenshots
    bg_color: QColor | None = None  # None = no background fill
    outline: bool = False
    outline_color: QColor | None = None  # stroke/outline around text
    # for pixelate/blur: store processed pixmap of the rect region after apply
    baked: QImage | None = None


# Common emoji stamps for annotation (Unicode — no asset pack needed)
EMOJI_PALETTE: list[str] = [
    "😀", "😁", "😂", "🤣", "😊", "😍", "🥰", "😘", "😎", "🤔",
    "😅", "😢", "😭", "😡", "🤯", "😴", "🥳", "😇", "🤩", "😏",
    "👍", "👎", "👏", "🙏", "💪", "🤝", "✌️", "👌", "🤞", "👋",
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "💔", "💯", "✨",
    "🔥", "⭐", "🌟", "⚡", "💥", "🎉", "🎊", "🎈", "🎁", "🏆",
    "✅", "❌", "⚠️", "❗", "❓", "💡", "📌", "📍", "📎", "📝",
    "👀", "💬", "💭", "📢", "🔔", "🚀", "🎯", "🐛", "🛠️", "💻",
    "📱", "🖥️", "⏱️", "📅", "🔒", "🔑", "💰", "📈", "📉", "🧾",
]


# In-canvas dock tools (drawn around selection like Flameshot — always visible)
# kind: tool | action | width_minus | width_plus | color | color_more
# key, icon_id (for vector draw), tooltip, kind
DOCK_TOOL_ROW: list[tuple[str, str, str, str]] = [
    ("pen", "pen", "画笔", "tool"),
    ("marker", "marker", "高亮", "tool"),
    ("arrow", "arrow", "箭头", "tool"),
    ("rect", "rect", "矩形", "tool"),
    ("ellipse", "ellipse", "椭圆", "tool"),
    ("fill", "fill", "色块", "tool"),
    ("text", "text", "文字", "tool"),
    ("number", "number", "序号", "tool"),
    ("emoji", "emoji", "表情", "tool"),
    ("pixelate", "pixelate", "马赛克", "tool"),
    ("blur", "blur", "模糊", "tool"),
]
# Default editor action shortcuts (overridable via cfg / 截图设置)
DEFAULT_EDITOR_SHORTCUTS: dict[str, str] = {
    "copy": "Ctrl+C",
    "save": "Ctrl+S",
    "pin": "Ctrl+P",
    "upload": "Ctrl+U",
    "undo": "Ctrl+Z",
    "redo": "Ctrl+Y",
    "accept": "Return",
    "cancel": "Esc",
}

DOCK_ACTION_ROW: list[tuple[str, str, str, str]] = [
    ("undo", "undo", "撤销", "action"),
    ("redo", "redo", "重做", "action"),
    ("reselect", "reselect", "重选区域", "action"),
    ("copy", "copy", "复制", "action"),
    ("save", "save", "保存", "action"),
    ("upload", "upload", "上传云端", "action"),
    ("pin", "pin", "钉住", "action"),
    ("accept", "accept", "完成", "action"),
    ("cancel", "cancel", "取消", "action"),
]
DOCK_COLORS = [
    QColor("#ff0000"),  # pure red — default pen
    QColor(249, 115, 22),
    QColor(234, 179, 8),
    QColor(34, 197, 94),
    QColor(14, 165, 233),
    QColor(59, 130, 246),
    QColor(168, 85, 247),
    QColor(255, 255, 255),
    QColor(15, 23, 42),
]


class PinnedShot(QWidget):
    """Pin screenshot to desktop (always on top, draggable)."""

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._pm = pixmap
        self.resize(pixmap.size())
        self._drag = False
        self._origin = QPoint()
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setPen(QPen(QColor(56, 189, 248), 2))
        p.setBrush(QBrush(self._pm))
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)
        p.drawPixmap(1, 1, self.width() - 2, self.height() - 2, self._pm)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._origin = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.MouseButton.RightButton:
            self.close()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag:
            self.move(e.globalPosition().toPoint() - self._origin)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        self._drag = False

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        self.close()


class ScreenshotEditor(QWidget):
    """Fullscreen selection + annotation (Flameshot workflow)."""

    finished = pyqtSignal(object)  # QImage | None

    def __init__(
        self,
        bg: QPixmap,
        desk_geo: QRect,
        *,
        mode: str = "region",  # region | full
        cfg: dict | None = None,
        on_upload: Callable[[Path], str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.bg = bg
        self.desk_geo = desk_geo
        self.mode = mode
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.on_upload = on_upload
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(desk_geo)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Keep mouse events even after clicking toolbar buttons
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Tool windows often miss wheel without focus — track app-level wheels while open
        self._wheel_filter_installed = False

        # phase: "select" = aim crosshair + drag region; "edit" = multi-tool annotation
        # (Flameshot-style: stay in edit until save/exit/cancel — never auto re-select)
        self.phase = "select" if mode == "region" else "edit"
        # IMPORTANT: selecting starts False — only True while left-button drag.
        # Previously True on open made any mouse-move draw a huge box from (0,0).
        self.selecting = False
        self.sel_origin = QPoint()
        self.sel = QRect()
        if mode == "full":
            self.sel = QRect(0, 0, desk_geo.width(), desk_geo.height())
            self.selecting = False
            self.phase = "edit"

        self.tool = "pen"
        # Defaults: red #ff0000, width 6 — then restore user prefs from cfg
        self.color = QColor("#ff0000")
        self.pen_w = 6  # brush thickness (not QWidget.width)
        self.text_bg = True
        self.text_bg_color = QColor(0, 0, 0, 210)
        self.text_outline = True
        self.text_outline_color = QColor(0, 0, 0, 255)
        self._load_annot_prefs()
        self.drawing = False
        self.cur_stroke: Stroke | None = None
        self.strokes: list[Stroke] = []
        self.redo_stack: list[Stroke] = []
        self.number_seq = 1  # kept in sync via _next_number()
        # Track cursor for Flameshot-style crosshair (local coords)
        try:
            gp = QCursor.pos()
            self.hover = QPoint(gp.x() - desk_geo.x(), gp.y() - desk_geo.y())
        except Exception:
            self.hover = QPoint(desk_geo.width() // 2, desk_geo.height() // 2)
        self._status_hint = ""
        self._last_upload_link = ""
        # Painted Flameshot-style dock around selection: list of (key, label, kind, QRect)
        # dock hits: key, icon_id, tip, kind, rect
        self._dock_hits: list[tuple[str, str, str, str, QRect]] = []
        self._dock_panel = QRect()
        self._hover_dock_key = ""
        self._text_edit: QLineEdit | None = None
        self._text_panel: QWidget | None = None
        self._text_anchor = QPoint()
        self._text_preview: str = ""  # live WYSIWYG draft while typing
        self._emoji_char = "😀"
        self._emoji_panel: QWidget | None = None

        self._pinned: list[PinnedShot] = []
        self._action_shortcuts: list[QShortcut] = []
        self._editor_shortcut_map = self._load_editor_shortcuts()

        # Shortcuts must NOT steal Enter while typing annotation text.
        # ApplicationShortcut so Tool-window focus quirks still get Ctrl+C etc.
        self._install_action_shortcuts()

        if mode == "full":
            QTimer.singleShot(0, self._enter_edit_mode)
        else:
            # Region mode: blank cursor so painted crosshair is the only aim
            self.setCursor(Qt.CursorShape.BlankCursor)
            QTimer.singleShot(0, self._sync_hover_from_global)

    def _sync_hover_from_global(self) -> None:
        try:
            gp = QCursor.pos()
            self.hover = QPoint(gp.x() - self.desk_geo.x(), gp.y() - self.desk_geo.y())
            self.update()
        except Exception:
            pass

    def _load_editor_shortcuts(self) -> dict[str, str]:
        out = dict(DEFAULT_EDITOR_SHORTCUTS)
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        for act in ("copy", "save", "pin", "upload", "undo", "redo"):
            key = f"shortcut_{act}"
            val = str(cfg.get(key) or "").strip()
            if val:
                out[act] = val
        return out

    def _shortcut_label(self, act: str) -> str:
        return (self._editor_shortcut_map.get(act) or "").strip()

    def _tip_with_shortcut(self, base: str, act: str) -> str:
        sc = self._shortcut_label(act)
        return f"{base} ({sc})" if sc else base

    def _install_action_shortcuts(self) -> None:
        for sc in self._action_shortcuts:
            try:
                sc.setParent(None)
                sc.deleteLater()
            except Exception:
                pass
        self._action_shortcuts.clear()

        def _bind(seq: str, handler) -> None:
            seq = (seq or "").strip()
            if not seq:
                return
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(handler)
            self._action_shortcuts.append(sc)

        m = self._editor_shortcut_map
        _bind(m.get("copy", "Ctrl+C"), lambda: self._shortcut_action("copy"))
        _bind(m.get("save", "Ctrl+S"), lambda: self._shortcut_action("save"))
        _bind(m.get("pin", "Ctrl+P"), lambda: self._shortcut_action("pin"))
        _bind(m.get("upload", "Ctrl+U"), lambda: self._shortcut_action("upload"))
        _bind(m.get("undo", "Ctrl+Z"), lambda: self._shortcut_action("undo"))
        _bind(m.get("redo", "Ctrl+Y"), lambda: self._shortcut_action("redo"))
        _bind("Ctrl+Shift+Z", lambda: self._shortcut_action("redo"))
        _bind(m.get("cancel", "Esc"), self._on_esc_shortcut)
        self._sc_return = QShortcut(QKeySequence("Return"), self)
        self._sc_return.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._sc_return.activated.connect(self._on_enter_shortcut)
        self._action_shortcuts.append(self._sc_return)
        self._sc_enter = QShortcut(QKeySequence("Enter"), self)
        self._sc_enter.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._sc_enter.activated.connect(self._on_enter_shortcut)
        self._action_shortcuts.append(self._sc_enter)

    def _shortcut_action(self, act: str) -> None:
        if self._text_input_active():
            return
        if self.phase == "select" and act in ("copy", "save", "pin", "upload", "accept"):
            if self.sel.isNull() or self.sel.width() < 4:
                self._status_hint = "请先框选区域，再使用快捷键"
                self.update()
                return
            self._enter_edit_mode()
        self._on_action(act)

    def _set_action_shortcuts_enabled(self, enabled: bool) -> None:
        for sc in self._action_shortcuts:
            try:
                sc.setEnabled(enabled)
            except Exception:
                pass

    def _set_tool(self, t: str) -> None:
        self.tool = t
        self.drawing = False
        self.cur_stroke = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        if t == "emoji":
            self._status_hint = f"表情 {self._emoji_char} · 点选区盖章 · 可再开面板换表情"
            self._show_emoji_panel()
        else:
            self._hide_emoji_panel()
            self._status_hint = f"当前工具：{t} · 在选区内拖动画图"
        self._rebuild_dock()
        self.update()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    @staticmethod
    def _color_to_rgba(c: QColor) -> list[int]:
        return [int(c.red()), int(c.green()), int(c.blue()), int(c.alpha())]

    @staticmethod
    def _color_from_cfg(val, default: QColor) -> QColor:
        """Load color from list [r,g,b,a] or hex string."""
        try:
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                a = int(val[3]) if len(val) > 3 else 255
                c = QColor(int(val[0]), int(val[1]), int(val[2]), a)
                return c if c.isValid() else QColor(default)
            if isinstance(val, str) and val.strip():
                c = QColor(val.strip())
                return c if c.isValid() else QColor(default)
        except Exception:
            pass
        return QColor(default)

    def _pick_color_dialog(self, initial: QColor, title: str, *, alpha: bool = False) -> QColor | None:
        """Color picker that stays above the fullscreen Tool overlay.

        Parent is None so the dialog is a normal top-level window (not buried
        under the screenshot Tool surface).
        """
        opts = QColorDialog.ColorDialogOption(0)
        if alpha:
            opts |= QColorDialog.ColorDialogOption.ShowAlphaChannel
        # Temporarily drop stay-on-top so the dialog is usable
        old_flags = self.windowFlags()
        try:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            self.show()
            c = QColorDialog.getColor(initial, None, title, opts)
        finally:
            try:
                self.setWindowFlags(old_flags)
                self.show()
                self.raise_()
                self.activateWindow()
            except Exception:
                pass
        return c if c.isValid() else None

    def _load_annot_prefs(self) -> None:
        """Restore pen color/width + text label style from screenshot cfg."""
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        self.color = self._color_from_cfg(cfg.get("annot_color"), QColor("#ff0000"))
        try:
            self.pen_w = max(1, min(40, int(cfg.get("annot_width") or 6)))
        except (TypeError, ValueError):
            self.pen_w = 6
        self.text_bg = bool(cfg.get("annot_text_bg", True))
        self.text_bg_color = self._color_from_cfg(
            cfg.get("annot_text_bg_rgba") or cfg.get("annot_text_bg_color"),
            QColor(0, 0, 0, 210),
        )
        if self.text_bg_color.alpha() < 40:
            self.text_bg_color.setAlpha(210)
        self.text_outline = bool(cfg.get("annot_text_outline", True))
        self.text_outline_color = self._color_from_cfg(
            cfg.get("annot_text_outline_rgba") or cfg.get("annot_text_outline_color"),
            QColor(0, 0, 0, 255),
        )

    def _save_annot_prefs(self) -> None:
        """Persist preferences so next capture keeps color/width/text style."""
        if not isinstance(self.cfg, dict):
            return
        self.cfg["annot_color"] = self._color_to_rgba(self.color)
        self.cfg["annot_width"] = int(self.pen_w)
        self.cfg["annot_text_bg"] = bool(self.text_bg)
        self.cfg["annot_text_bg_rgba"] = self._color_to_rgba(self.text_bg_color)
        self.cfg["annot_text_outline"] = bool(self.text_outline)
        self.cfg["annot_text_outline_rgba"] = self._color_to_rgba(self.text_outline_color)

    def _set_color(self, c: QColor) -> None:
        self.color = QColor(c)
        self._save_annot_prefs()
        self._status_hint = f"颜色 {self.color.name()}（已记住）"
        if self._text_input_active():
            self._refresh_text_swatches()
        self.update()

    def _set_width(self, w: int) -> None:
        old = self.pen_w
        self.pen_w = max(1, min(40, int(w)))
        self._save_annot_prefs()
        if self.pen_w == old and self.phase == "edit":
            self._status_hint = f"粗细 {self.pen_w} · 滚轮可调（已记住）"
            self.update()
            return
        self._status_hint = f"粗细 {self.pen_w} · 滚轮可调（已记住）"
        if self.phase == "edit":
            try:
                self._rebuild_dock()
            except Exception:
                pass
        if self._text_input_active():
            self._refresh_text_swatches()
            self._status_hint = f"字号(粗细) {self.pen_w} · 实时预览中"
        self.update()

    def _next_number(self) -> int:
        """Next serial for number stamps — always consecutive among current strokes.

        Undo removes a number; the next stamp reuses that slot instead of skipping.
        """
        nums = [int(st.number) for st in self.strokes if st.kind == "number" and int(st.number) > 0]
        n = (max(nums) if nums else 0) + 1
        self.number_seq = n
        return n

    def _install_wheel_filter(self) -> None:
        app = QApplication.instance()
        if app is None or self._wheel_filter_installed:
            return
        app.installEventFilter(self)
        self._wheel_filter_installed = True

    def _remove_wheel_filter(self) -> None:
        app = QApplication.instance()
        if app is None or not self._wheel_filter_installed:
            return
        try:
            app.removeEventFilter(self)
        except Exception:
            pass
        self._wheel_filter_installed = False

    def showEvent(self, e) -> None:  # type: ignore[override]
        super().showEvent(e)
        self._install_wheel_filter()
        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        self._remove_wheel_filter()
        super().closeEvent(e)

    def hideEvent(self, e) -> None:  # type: ignore[override]
        self._remove_wheel_filter()
        super().hideEvent(e)

    def enterEvent(self, e) -> None:  # type: ignore[override]
        super().enterEvent(e)
        self.activateWindow()
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def event(self, e) -> bool:  # type: ignore[override]
        if e.type() == QEvent.Type.Wheel:
            self.wheelEvent(e)  # type: ignore[arg-type]
            return True
        return super().event(e)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if not self.isVisible():
            return False
        if event.type() != QEvent.Type.Wheel:
            return False
        if self.phase != "edit":
            return False
        try:
            gp = QCursor.pos()
            if not self.frameGeometry().contains(gp):
                return False
        except Exception:
            pass
        self.wheelEvent(event)  # type: ignore[arg-type]
        return True

    def wheelEvent(self, e: QWheelEvent) -> None:  # type: ignore[override]
        """Mouse wheel adjusts brush thickness (edit mode; also live text size)."""
        if self.phase != "edit":
            try:
                super().wheelEvent(e)
            except Exception:
                pass
            return
        delta = int(e.angleDelta().y())
        if delta == 0:
            delta = int(e.angleDelta().x())
        if delta == 0:
            delta = int(e.pixelDelta().y())
        if delta == 0:
            delta = int(e.pixelDelta().x())
        if delta == 0:
            e.accept()
            return
        step = max(1, min(4, abs(delta) // 120 if abs(delta) >= 120 else 1))
        if delta > 0:
            self._set_width(self.pen_w + step)
        else:
            self._set_width(self.pen_w - step)
        e.accept()

    def _enter_edit_mode(self) -> None:
        """Lock selection and enable continuous multi-tool annotation."""
        self.phase = "edit"
        self.selecting = False
        self.drawing = False
        self.cur_stroke = None
        self.setCursor(Qt.CursorShape.CrossCursor)
        c = self._shortcut_label("copy") or "Ctrl+C"
        s = self._shortcut_label("save") or "Ctrl+S"
        p = self._shortcut_label("pin") or "Ctrl+P"
        u = self._shortcut_label("upload") or "Ctrl+U"
        self._status_hint = f"滚轮调粗细 · {c}复制 · {s}保存 · {p}图钉 · {u}上传 · Esc取消"
        self._rebuild_dock()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _enter_select_mode(self, *, clear_strokes: bool = True) -> None:
        """Flameshot-style aim mode: crosshair + coordinates, no box until drag."""
        if clear_strokes:
            self.strokes.clear()
            self.redo_stack.clear()
            self.number_seq = 1
        self.cur_stroke = None
        self.drawing = False
        self.sel = QRect()
        self.phase = "select"
        self.selecting = False
        self._dock_hits = []
        self._dock_panel = QRect()
        self.setCursor(Qt.CursorShape.BlankCursor)
        self._status_hint = "移动准星对准目标，按住左键拖选范围 · 空格=全屏 · Esc=取消"
        self._sync_hover_from_global()
        self.update()

    def _rebuild_dock(self) -> None:
        """Compute Flameshot-like tool dock rects around the selection (local coords)."""
        self._dock_hits = []
        self._dock_panel = QRect()
        if self.phase != "edit" or self.sel.isNull() or self.sel.width() < 4:
            return
        s = self.sel.normalized()
        btn_w, btn_h, gap = 42, 36, 4
        pad = 8

        def layout_row(items: list[tuple[str, str, str, str]], y: int) -> tuple[list, int, int]:
            n = len(items)
            row_w = n * btn_w + (n - 1) * gap + pad * 2
            x0 = s.center().x() - row_w // 2
            x0 = max(6, min(x0, self.width() - row_w - 6))
            hits = []
            x = x0 + pad
            for key, icon_id, tip, kind in items:
                label = tip
                if kind == "action" and key in self._editor_shortcut_map:
                    label = self._tip_with_shortcut(tip, key)
                hits.append((key, icon_id, label, kind, QRect(x, y, btn_w, btn_h)))
                x += btn_w + gap
            return hits, x0, row_w

        row1_h = btn_h
        row2_h = btn_h
        color_h = 28
        total_h = pad + row1_h + gap + row2_h + gap + color_h + pad
        below_y = s.bottom() + 10
        above_y = s.top() - total_h - 10
        if below_y + total_h <= self.height() - 6:
            panel_y = below_y
        elif above_y >= 6:
            panel_y = above_y
        else:
            panel_y = max(6, self.height() - total_h - 8)

        y1 = panel_y + pad
        hits1, x0, row_w = layout_row(DOCK_TOOL_ROW, y1)
        y2 = y1 + row1_h + gap
        hits2, x0b, row_w2 = layout_row(DOCK_ACTION_ROW, y2)
        panel_x = min(x0, x0b)
        panel_w = max(row_w, row_w2)

        y3 = y2 + row2_h + gap
        cx = panel_x + pad
        hits2.append(("width_minus", "w_minus", "更细", "width_minus", QRect(cx, y3, 36, color_h)))
        cx += 40
        hits2.append(("width_label", "w_label", str(self.pen_w), "width_label", QRect(cx, y3, 36, color_h)))
        cx += 40
        hits2.append(("width_plus", "w_plus", "更粗", "width_plus", QRect(cx, y3, 36, color_h)))
        cx += 48
        for i, col in enumerate(DOCK_COLORS):
            r = QRect(cx, y3 + 2, 24, 24)
            hits2.append((f"color_{i}", "color", col.name(), "color", r))
            cx += 28
        hits2.append(("color_more", "color_more", "更多颜色", "color_more", QRect(cx, y3, 32, color_h)))
        cx += 36
        panel_w = max(panel_w, cx - panel_x + pad)
        panel_w = min(panel_w, self.width() - 12)
        panel_x = max(6, min(panel_x, self.width() - panel_w - 6))

        self._dock_hits = hits1 + hits2
        self._dock_panel = QRect(panel_x, panel_y, panel_w, total_h)

    def _hit_dock(self, pos: QPoint) -> tuple[str, str, str, str] | None:
        for key, icon_id, tip, kind, rect in self._dock_hits:
            if rect.contains(pos):
                return key, icon_id, tip, kind
        return None

    def _draw_icon(self, p: QPainter, icon_id: str, rect: QRect, fg: QColor) -> None:
        """Vector icon for dock buttons (Flameshot-style recognition)."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = rect.center().x(), rect.center().y()
        pen = QPen(fg, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if icon_id == "pen":
            p.drawLine(cx - 8, cy + 8, cx + 8, cy - 8)
            p.setBrush(fg)
            p.drawEllipse(QPoint(cx + 8, cy - 8), 3, 3)
        elif icon_id == "marker":
            p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 100))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(cx - 10, cy - 6, 20, 12, 3, 3)
            p.setPen(pen)
            p.drawLine(cx - 10, cy + 8, cx + 10, cy + 8)
        elif icon_id == "arrow":
            p.drawLine(cx - 10, cy + 8, cx + 8, cy - 8)
            p.drawLine(cx + 8, cy - 8, cx + 2, cy - 8)
            p.drawLine(cx + 8, cy - 8, cx + 8, cy - 2)
        elif icon_id == "rect":
            p.drawRect(cx - 9, cy - 7, 18, 14)
        elif icon_id == "ellipse":
            p.drawEllipse(QPoint(cx, cy), 10, 7)
        elif icon_id == "fill":
            p.setBrush(fg)
            p.drawRect(cx - 9, cy - 7, 18, 14)
        elif icon_id == "text":
            p.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "T")
        elif icon_id == "number":
            p.setBrush(fg)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPoint(cx, cy), 9, 9)
            p.setPen(QPen(QColor(15, 23, 42), 2))
            p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "1")
        elif icon_id == "emoji":
            # Prefer color emoji fonts when available
            font = QFont("Segoe UI Emoji", 14)
            font.setFamilies(["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", "Segoe UI Symbol"])
            p.setFont(font)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "😀")
        elif icon_id == "pixelate":
            for i in range(3):
                for j in range(3):
                    if (i + j) % 2 == 0:
                        p.fillRect(cx - 9 + i * 6, cy - 9 + j * 6, 5, 5, fg)
        elif icon_id == "blur":
            p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 80))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPoint(cx, cy), 10, 10)
            p.setBrush(QColor(fg.red(), fg.green(), fg.blue(), 160))
            p.drawEllipse(QPoint(cx, cy), 5, 5)
        elif icon_id == "undo":
            path = QPainterPath()
            path.moveTo(cx + 6, cy - 6)
            path.arcTo(cx - 8, cy - 8, 16, 16, 30, 200)
            p.drawPath(path)
            p.drawLine(cx - 8, cy - 2, cx - 2, cy - 8)
            p.drawLine(cx - 8, cy - 2, cx - 2, cy + 2)
        elif icon_id == "redo":
            path = QPainterPath()
            path.moveTo(cx - 6, cy - 6)
            path.arcTo(cx - 8, cy - 8, 16, 16, 150, -200)
            p.drawPath(path)
            p.drawLine(cx + 8, cy - 2, cx + 2, cy - 8)
            p.drawLine(cx + 8, cy - 2, cx + 2, cy + 2)
        elif icon_id == "reselect":
            # Marquee / crop corners (distinct from copy)
            p.setPen(QPen(fg, 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.SquareCap))
            p.drawRect(cx - 9, cy - 7, 18, 14)
            p.setPen(QPen(fg, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
            # corner L marks
            for ox, oy, dx, dy in (
                (-9, -7, 5, 0),
                (-9, -7, 0, 5),
                (9, -7, -5, 0),
                (9, -7, 0, 5),
                (-9, 7, 5, 0),
                (-9, 7, 0, -5),
                (9, 7, -5, 0),
                (9, 7, 0, -5),
            ):
                p.drawLine(cx + ox, cy + oy, cx + ox + dx, cy + oy + dy)
        elif icon_id == "copy":
            # Clipboard: board + clipped page (clearly not reselect)
            p.setPen(QPen(fg, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            # board body
            p.drawRoundedRect(cx - 8, cy - 4, 16, 14, 2, 2)
            # clip on top
            p.drawRoundedRect(cx - 4, cy - 9, 8, 6, 2, 2)
            p.drawLine(cx - 3, cy + 1, cx + 3, cy + 1)
            p.drawLine(cx - 3, cy + 5, cx + 3, cy + 5)
        elif icon_id == "save":
            p.drawRoundedRect(cx - 8, cy - 8, 16, 16, 2, 2)
            p.drawRect(cx - 4, cy - 8, 8, 6)
            p.drawLine(cx - 3, cy + 2, cx + 3, cy + 2)
        elif icon_id == "upload":
            p.drawLine(cx, cy + 8, cx, cy - 6)
            p.drawLine(cx, cy - 6, cx - 5, cy)
            p.drawLine(cx, cy - 6, cx + 5, cy)
            p.drawLine(cx - 8, cy + 8, cx + 8, cy + 8)
        elif icon_id == "pin":
            p.drawEllipse(QPoint(cx, cy - 4), 5, 5)
            p.drawLine(cx, cy + 1, cx, cy + 9)
        elif icon_id == "accept":
            p.setPen(QPen(fg, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawLine(cx - 7, cy, cx - 2, cy + 6)
            p.drawLine(cx - 2, cy + 6, cx + 8, cy - 6)
        elif icon_id == "cancel":
            p.setPen(QPen(fg, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(cx - 7, cy - 7, cx + 7, cy + 7)
            p.drawLine(cx + 7, cy - 7, cx - 7, cy + 7)
        elif icon_id == "w_minus":
            p.setPen(QPen(fg, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(cx - 7, cy, cx + 7, cy)
        elif icon_id == "w_plus":
            p.setPen(QPen(fg, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(cx - 7, cy, cx + 7, cy)
            p.drawLine(cx, cy - 7, cx, cy + 7)
        elif icon_id == "color_more":
            p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "…")
        p.restore()

    def _paint_dock(self, p: QPainter) -> None:
        if not self._dock_hits:
            self._rebuild_dock()
        if not self._dock_hits or self._dock_panel.isNull():
            return
        panel = self._dock_panel
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(15, 23, 42, 250))
        p.setPen(QPen(QColor(56, 189, 248), 2))
        p.drawRoundedRect(panel, 10, 10)

        for key, icon_id, tip, kind, rect in self._dock_hits:
            if kind == "color":
                idx = int(key.split("_")[1])
                col = DOCK_COLORS[idx]
                p.setBrush(col)
                selected = col.rgb() == QColor(self.color).rgb()
                p.setPen(QPen(QColor(255, 255, 255) if selected else QColor(100, 116, 139), 2 if selected else 1))
                p.drawEllipse(rect)
                continue
            if kind == "width_label":
                p.setPen(QColor(125, 211, 252))
                p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(self.pen_w))
                continue

            active_tool = kind == "tool" and key == self.tool
            hover = key == self._hover_dock_key
            if key == "cancel":
                bg = QColor(220, 38, 38, 240)
                fg = QColor(255, 255, 255)
            elif key in ("accept", "upload"):
                bg = QColor(14, 165, 233, 250)
                fg = QColor(15, 23, 42)
            elif active_tool or hover:
                bg = QColor(56, 189, 248, 250)
                fg = QColor(15, 23, 42)
            else:
                bg = QColor(30, 41, 59, 250)
                fg = QColor(226, 232, 240)
            p.setBrush(bg)
            p.setPen(QPen(QColor(51, 65, 85), 1))
            p.drawRoundedRect(rect, 8, 8)
            self._draw_icon(p, icon_id, rect, fg)

        # hover tooltip under panel
        if self._hover_dock_key:
            for key, icon_id, tip, kind, rect in self._dock_hits:
                if key == self._hover_dock_key and tip:
                    p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                    p.setPen(QColor(253, 224, 71))
                    p.drawText(panel.left() + 10, panel.bottom() - 4, tip)
                    break

        p.setBrush(self.color)
        p.setPen(QPen(Qt.GlobalColor.white, 2))
        p.drawEllipse(panel.right() - 22, panel.top() + 8, 14, 14)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self.bg)

        # ---------- SELECT: Flameshot crosshair + coordinates ----------
        if self.phase == "select":
            dim = QColor(0, 0, 0, 120)
            hx = max(0, min(self.hover.x(), self.width() - 1))
            hy = max(0, min(self.hover.y(), self.height() - 1))
            if self.selecting and not self.sel.isNull() and self.sel.width() > 0:
                s = self.sel.normalized()
                r = self.rect()
                p.fillRect(0, 0, r.width(), s.top(), dim)
                p.fillRect(0, s.bottom() + 1, r.width(), r.height() - s.bottom() - 1, dim)
                p.fillRect(0, s.top(), s.left(), s.height(), dim)
                p.fillRect(s.right() + 1, s.top(), r.width() - s.right() - 1, s.height(), dim)
                p.setPen(QPen(QColor(56, 189, 248), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(s)
                p.setPen(QColor(255, 255, 255))
                p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
                p.drawText(s.left() + 6, max(20, s.top() - 8), f"{s.width()} × {s.height()}  px")
            else:
                p.fillRect(self.rect(), dim)
                p.setPen(QColor(226, 232, 240, 220))
                p.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
                p.drawText(
                    self.rect().adjusted(0, -100, 0, 0),
                    Qt.AlignmentFlag.AlignCenter,
                    "十字准星瞄准 · 按住左键拖出选区\n空格 = 全屏 · Esc = 取消",
                )

            # Full-screen crosshair (high contrast)
            p.setPen(QPen(QColor(14, 165, 233), 1))
            p.drawLine(0, hy, self.width(), hy)
            p.drawLine(hx, 0, hx, self.height())
            p.setPen(QPen(QColor(255, 255, 255), 1, Qt.PenStyle.DashLine))
            p.drawLine(0, hy, self.width(), hy)
            p.drawLine(hx, 0, hx, self.height())
            # Aim ring
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(239, 68, 68), 2))
            p.drawEllipse(QPointF(hx, hy), 8, 8)
            p.setBrush(QColor(239, 68, 68))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(hx, hy), 3, 3)

            # Large coordinate HUD (always visible near cursor + top-left)
            gx = self.desk_geo.x() + hx
            gy = self.desk_geo.y() + hy
            badge = f"  X = {gx}    Y = {gy}  "
            p.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
            fm = p.fontMetrics()
            bw = fm.horizontalAdvance(badge) + 16
            bh = fm.height() + 14
            bx = min(max(12, hx + 18), self.width() - bw - 12)
            by = min(max(12, hy + 18), self.height() - bh - 12)
            p.setBrush(QColor(15, 23, 42, 235))
            p.setPen(QPen(QColor(56, 189, 248), 2))
            p.drawRoundedRect(bx, by, bw, bh, 8, 8)
            p.setPen(QColor(125, 211, 252))
            p.drawText(bx + 8, by + bh - 10, badge)
            # Fixed corner readout
            p.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
            corner = f"坐标  X:{gx}  Y:{gy}"
            p.setBrush(QColor(15, 23, 42, 220))
            p.setPen(QPen(QColor(56, 189, 248), 1))
            p.drawRoundedRect(16, 16, 220, 36, 8, 8)
            p.setPen(QColor(255, 255, 255))
            p.drawText(28, 40, corner)
            return

        # ---------- EDIT: dim outside + annotations + dock around selection ----------
        if not self.sel.isNull() and self.sel.width() > 0:
            dim = QColor(0, 0, 0, 140)
            r = self.rect()
            s = self.sel.normalized()
            p.fillRect(0, 0, r.width(), s.top(), dim)
            p.fillRect(0, s.bottom() + 1, r.width(), r.height() - s.bottom() - 1, dim)
            p.fillRect(0, s.top(), s.left(), s.height(), dim)
            p.fillRect(s.right() + 1, s.top(), r.width() - s.right() - 1, s.height(), dim)
            p.setPen(QPen(QColor(56, 189, 248), 2, Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(s)
            p.setPen(QColor(255, 255, 255))
            p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
            label = f"{s.width()} × {s.height()}  ·  已标注 {len(self.strokes)} 笔"
            p.drawText(s.left() + 4, max(16, s.top() - 8), label)

        # Annotations clipped to selection
        if not self.sel.isNull():
            p.save()
            p.setClipRect(self.sel.normalized())
            for st in self.strokes:
                self._paint_stroke(p, st)
            if self.cur_stroke:
                self._paint_stroke(p, self.cur_stroke)
            self._paint_text_live_preview(p)
            p.restore()
        else:
            for st in self.strokes:
                self._paint_stroke(p, st)
            if self.cur_stroke:
                self._paint_stroke(p, self.cur_stroke)
            self._paint_text_live_preview(p)

        # Flameshot-style tools painted around selection (always on top of dim)
        self._paint_dock(p)
        if self._status_hint and not self.sel.isNull():
            s = self.sel.normalized()
            p.setPen(QColor(125, 211, 252))
            p.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
            # Put status opposite the dock
            if not self._dock_panel.isNull() and self._dock_panel.top() >= s.bottom():
                ty = max(16, s.top() - 8)
            else:
                ty = min(self.height() - 12, s.bottom() + 18)
            p.drawText(s.left() + 4, ty, self._status_hint)

    def _paint_stroke(self, p: QPainter, st: Stroke) -> None:
        if st.kind in ("pixelate", "blur") and st.baked is not None and len(st.points) >= 2:
            r = QRectF(st.points[0], st.points[1]).normalized().toRect()
            p.drawImage(r.topLeft(), st.baked)
            return
        col = QColor(st.color)
        if st.kind == "marker":
            col.setAlpha(90)
        pen = QPen(col, st.width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        if st.kind in ("pen", "marker"):
            if len(st.points) >= 2:
                path = self._smooth_stroke_path(st.points)
                p.drawPath(path)
            elif len(st.points) == 1:
                r = max(0.5, st.width / 2.0)
                p.setBrush(QBrush(col))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(st.points[0], r, r)
        elif st.kind == "arrow" and len(st.points) >= 2:
            a, b = st.points[0], st.points[-1]
            p.drawLine(a, b)
            self._draw_arrow_head(p, a, b, col, st.width)
        elif st.kind in ("rect", "fill", "ellipse") and len(st.points) >= 2:
            r = QRectF(st.points[0], st.points[1]).normalized()
            if st.kind == "fill":
                fill = QColor(col)
                fill.setAlpha(180)
                p.setBrush(fill)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(r)
            elif st.kind == "rect":
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(r)
            else:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(r)
        elif st.kind == "text" and st.points:
            self._paint_text_label(p, st, col)
        elif st.kind == "emoji" and st.points:
            self._paint_emoji(p, st)
        elif st.kind == "number" and st.points:
            r = 12 + st.width
            c = st.points[0]
            p.setBrush(col)
            p.setPen(QPen(Qt.GlobalColor.white, 2))
            p.drawEllipse(c, r, r)
            p.setPen(Qt.GlobalColor.white)
            p.setFont(QFont("Segoe UI", max(10, int(r)), QFont.Weight.Bold))
            p.drawText(QRectF(c.x() - r, c.y() - r, r * 2, r * 2), Qt.AlignmentFlag.AlignCenter, str(st.number))
        elif st.kind in ("pixelate", "blur") and len(st.points) >= 2:
            r = QRectF(st.points[0], st.points[1]).normalized()
            p.setPen(QPen(col, 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r)

    def _text_font_for_width(self, width: float) -> QFont:
        fs = max(12, int(float(width) * 3.2))
        font = QFont("Microsoft YaHei UI", fs, QFont.Weight.Bold)
        if not font.exactMatch():
            font = QFont("Segoe UI", fs, QFont.Weight.Bold)
        return font

    def _emoji_font_for_width(self, width: float) -> QFont:
        fs = max(18, int(float(width) * 5.5))
        font = QFont("Segoe UI Emoji", fs)
        font.setFamilies(
            ["Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", "Segoe UI Symbol", "Segoe UI"]
        )
        return font

    def _paint_emoji(self, p: QPainter, st: Stroke) -> None:
        ch = (st.text or "").strip() or "😀"
        font = self._emoji_font_for_width(st.width)
        p.setFont(font)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        fm = p.fontMetrics()
        pt = st.points[0]
        size = max(fm.height(), fm.horizontalAdvance(ch), 20)
        box = QRectF(pt.x() - size / 2.0, pt.y() - size / 2.0, size, size)
        p.setPen(Qt.GlobalColor.black)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, ch)

    def _hide_emoji_panel(self) -> None:
        if self._emoji_panel is not None:
            try:
                self._emoji_panel.hide()
                self._emoji_panel.deleteLater()
            except Exception:
                pass
            self._emoji_panel = None

    def _pick_emoji(self, ch: str) -> None:
        self._emoji_char = ch or "😀"
        self._status_hint = f"表情 {self._emoji_char} · 在选区点击盖章 · 滚轮调大小"
        self.update()

    def _show_emoji_panel(self) -> None:
        """Floating emoji palette near selection (does not block stamping)."""
        self._hide_emoji_panel()
        panel = QWidget(self)
        panel.setObjectName("emojiPanel")
        panel.setStyleSheet(
            """
            QWidget#emojiPanel {
                background: rgba(15, 23, 42, 0.97);
                border: 2px solid #38bdf8;
                border-radius: 10px;
            }
            QPushButton {
                background: #1e293b; border: 1px solid #334155; border-radius: 6px;
                font-size: 18px; min-width: 34px; min-height: 34px; padding: 2px;
            }
            QPushButton:hover { background: #0ea5e9; border-color: #38bdf8; }
            QPushButton#soft {
                background: #334155; color: #e2e8f0; font-size: 12px; font-weight: 700;
                min-width: 64px; min-height: 28px;
            }
            QLabel#hint { color: #94a3b8; font-size: 11px; }
            """
        )
        root = QVBoxLayout(panel)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addWidget(QLabel(f"当前：{self._emoji_char}  ·  点表情选用，再点截图盖章", objectName="hint"))

        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(168)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setSpacing(4)
        grid.setContentsMargins(2, 2, 2, 2)
        cols = 10
        for i, ch in enumerate(EMOJI_PALETTE):
            btn = QPushButton(ch)
            btn.setToolTip(ch)
            btn.clicked.connect(lambda _=False, c=ch: self._pick_emoji(c))
            grid.addWidget(btn, i // cols, i % cols)
        scroll.setWidget(holder)
        root.addWidget(scroll)

        row = QHBoxLayout()
        btn_close = QPushButton("收起面板", objectName="soft")
        btn_close.clicked.connect(self._hide_emoji_panel)
        row.addWidget(btn_close)
        row.addStretch(1)
        root.addLayout(row)

        panel.adjustSize()
        pw = max(panel.sizeHint().width(), 390)
        ph = max(panel.sizeHint().height(), 230)
        panel.resize(pw, ph)
        s = self.sel.normalized()
        if s.isNull() or s.width() < 4:
            s = self.rect()
        x = max(6, min(s.left() + 8, self.width() - pw - 6))
        y = max(6, min(s.bottom() - ph - 8 if s.bottom() - ph - 8 > s.top() else s.top() + 8, self.height() - ph - 6))
        # Prefer below dock if dock is above selection bottom
        if not self._dock_panel.isNull():
            y = min(max(6, self._dock_panel.bottom() + 8), self.height() - ph - 6)
            x = max(6, min(self._dock_panel.left(), self.width() - pw - 6))
        panel.move(x, y)
        panel.show()
        panel.raise_()
        self._emoji_panel = panel
        self._status_hint = f"表情 {self._emoji_char} · 先点表情再点选区盖章"

    def _paint_text_label(self, p: QPainter, st: Stroke, col: QColor) -> None:
        """Draw text with optional solid background + outline (readable on clutter)."""
        text = st.text or ""
        if not text:
            return
        font = self._text_font_for_width(st.width)
        p.setFont(font)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        fm = p.fontMetrics()
        pt = st.points[0]
        # Baseline at pt; box from advance + ascent/descent (stable for CJK)
        adv = max(1, fm.horizontalAdvance(text))
        ascent = fm.ascent()
        descent = fm.descent()
        pad_x = max(4, int(st.width * 0.55))
        pad_y = max(3, int(st.width * 0.45))
        box = QRectF(
            pt.x() - pad_x,
            pt.y() - ascent - pad_y,
            adv + pad_x * 2,
            ascent + descent + pad_y * 2,
        )
        # Background plate
        if st.bg_color is not None:
            bg = QColor(st.bg_color)
            if bg.alpha() < 30:
                bg.setAlpha(200)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(bg))
            p.drawRoundedRect(box, 4, 4)
        # Outline then fill (works better for CJK than strokePath alone on some fonts)
        if st.outline:
            oc = QColor(st.outline_color) if st.outline_color is not None else QColor(0, 0, 0)
            if oc.alpha() < 30:
                oc.setAlpha(255)
            # Cap outline so thick brush doesn't explode into a huge black blob
            ow = max(1.2, min(3.5, float(st.width) * 0.35))
            p.setPen(oc)
            for dx, dy in (
                (-ow, 0),
                (ow, 0),
                (0, -ow),
                (0, ow),
                (-ow, -ow),
                (ow, -ow),
                (-ow, ow),
                (ow, ow),
            ):
                p.drawText(QPointF(pt.x() + dx, pt.y() + dy), text)
        p.setPen(col)
        p.drawText(pt, text)

    def _make_text_preview_stroke(self) -> Stroke | None:
        """Current typing draft as a stroke (same paint path as final)."""
        if not self._text_input_active():
            return None
        text = self._text_preview
        if self._text_edit is not None:
            text = self._text_edit.text()
        text = text if text is not None else ""
        # Keep spaces while typing; strip only on commit
        if not text:
            return None
        bg = QColor(self.text_bg_color) if self.text_bg else None
        if bg is not None and bg.alpha() < 40:
            bg.setAlpha(200)
        ol_c = QColor(self.text_outline_color) if self.text_outline else None
        return Stroke(
            kind="text",
            points=[QPointF(self._text_anchor)],
            color=QColor(self.color),
            width=float(self.pen_w),
            text=text,
            bg_color=bg,
            outline=bool(self.text_outline),
            outline_color=ol_c,
        )

    def _paint_text_live_preview(self, p: QPainter) -> None:
        """WYSIWYG preview at click anchor while the text panel is open."""
        if not self._text_input_active():
            return
        st = self._make_text_preview_stroke()
        if st is not None:
            self._paint_text_label(p, st, QColor(st.color))
            # Soft dashed ring so user sees this is draft, not committed
            try:
                font = self._text_font_for_width(st.width)
                p.setFont(font)
                fm = p.fontMetrics()
                adv = max(1, fm.horizontalAdvance(st.text or ""))
                ascent = fm.ascent()
                descent = fm.descent()
                pad = 6
                box = QRectF(
                    self._text_anchor.x() - pad,
                    self._text_anchor.y() - ascent - pad,
                    adv + pad * 2,
                    ascent + descent + pad * 2,
                )
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(56, 189, 248, 180), 1, Qt.PenStyle.DashLine))
                p.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 5, 5)
            except Exception:
                pass
            return
        # Empty: show insertion caret + hint at anchor
        ax, ay = self._text_anchor.x(), self._text_anchor.y()
        fs = max(12, int(self.pen_w * 3.2))
        p.setPen(QPen(QColor(56, 189, 248), 2))
        p.drawLine(ax, ay - fs, ax, ay + 4)
        p.setPen(QColor(148, 163, 184))
        p.setFont(QFont("Microsoft YaHei UI", 11))
        p.drawText(ax + 6, ay, "在此输入…（实时预览）")

    def _draw_arrow_head(self, p: QPainter, a: QPointF, b: QPointF, col: QColor, w: float) -> None:
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        size = 10 + w * 1.5
        p1 = QPointF(b.x() - size * math.cos(ang - 0.4), b.y() - size * math.sin(ang - 0.4))
        p2 = QPointF(b.x() - size * math.cos(ang + 0.4), b.y() - size * math.sin(ang + 0.4))
        path = QPainterPath(b)
        path.lineTo(p1)
        path.lineTo(p2)
        path.closeSubpath()
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(path)

    def _text_input_active(self) -> bool:
        return self._text_edit is not None or self._text_panel is not None

    def _on_esc_shortcut(self) -> None:
        if self._text_input_active():
            self._cancel_text_input()
            return
        self._on_action("cancel")

    def _on_enter_shortcut(self) -> None:
        # Enter while typing = confirm text only (never exit the whole editor)
        if self._text_input_active():
            self._commit_text_input()
            return
        self._on_action("accept")

    def keyPressEvent(self, e) -> None:
        if self._text_input_active() and e.key() == Qt.Key.Key_Escape:
            self._cancel_text_input()
            return
        if self._text_input_active() and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit_text_input()
            e.accept()
            return
        if e.key() == Qt.Key.Key_Space and self.phase == "select":
            self.sel = self.rect()
            self._enter_edit_mode()
            return
        super().keyPressEvent(e)

    def _clamp_to_sel(self, pos: QPoint) -> QPoint:
        s = self.sel.normalized()
        if s.isNull() or s.width() < 2:
            return pos
        return QPoint(
            max(s.left(), min(pos.x(), s.right())),
            max(s.top(), min(pos.y(), s.bottom())),
        )

    def _handle_dock_click(self, key: str, kind: str) -> bool:
        """Handle in-canvas dock button. Returns True if consumed."""
        if kind == "tool":
            self._set_tool(key)
            return True
        if kind == "action":
            self._on_action(key)
            return True
        if kind == "width_minus":
            self._set_width(self.pen_w - 1)
            self._rebuild_dock()
            return True
        if kind == "width_plus":
            self._set_width(self.pen_w + 1)
            self._rebuild_dock()
            return True
        if kind == "width_label":
            return True
        if kind == "color":
            idx = int(key.split("_")[1])
            self._set_color(DOCK_COLORS[idx])
            self._rebuild_dock()
            return True
        if kind == "color_more":
            c = self._pick_color_dialog(self.color, "选择画笔/文字颜色", alpha=False)
            if c is not None:
                self._set_color(c)
                self._rebuild_dock()
            return True
        return False

    def _swatch_style(self, c: QColor) -> str:
        return (
            f"background: {c.name()}; border: 1px solid #94a3b8; border-radius: 4px; "
            f"min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px;"
        )

    def _refresh_text_swatches(self) -> None:
        if getattr(self, "_sw_font", None) is not None:
            self._sw_font.setStyleSheet(self._swatch_style(self.color))
        if getattr(self, "_sw_bg", None) is not None:
            self._sw_bg.setStyleSheet(self._swatch_style(self.text_bg_color))
        if getattr(self, "_sw_ol", None) is not None:
            self._sw_ol.setStyleSheet(self._swatch_style(self.text_outline_color))
        if getattr(self, "_text_edit", None) is not None and self._text_edit is not None:
            fs = max(14, int(self.pen_w * 3))
            self._text_edit.setStyleSheet(
                f"""
                QLineEdit {{
                    background: #020617;
                    color: {self.color.name()};
                    border: 1px solid #38bdf8;
                    border-radius: 6px;
                    padding: 6px 8px;
                    font-size: {fs}px;
                    font-weight: 700;
                    selection-background-color: #0ea5e9;
                }}
                """
            )

    def _place_text_panel(self, panel: QWidget, anchor: QPoint, pw: int, ph: int) -> None:
        """Put control panel near selection but not covering the live text preview."""
        s = self.sel.normalized()
        if s.isNull() or s.width() < 4:
            s = self.rect()
        gap = 16
        candidates = [
            # Prefer under / right of click so WYSIWYG at anchor stays visible
            QPoint(anchor.x(), anchor.y() + gap + 8),
            QPoint(anchor.x() - pw // 2, anchor.y() + gap + 8),
            QPoint(anchor.x(), anchor.y() - ph - gap),
            QPoint(s.left() + 8, s.bottom() - ph - 8),
            QPoint(s.right() - pw - 8, s.bottom() - ph - 8),
            QPoint(s.left() + 8, s.top() + 8),
            QPoint(s.right() - pw - 8, s.top() + 8),
        ]
        chosen = None
        for c in candidates:
            x = max(s.left() + 2, min(c.x(), max(s.left() + 2, s.right() - pw - 2)))
            y = max(s.top() + 2, min(c.y(), max(s.top() + 2, s.bottom() - ph - 2)))
            rect = QRect(x, y, pw, ph)
            # Keep a margin around anchor so preview isn't hidden under the panel
            if rect.adjusted(-8, -8, 8, 8).contains(anchor):
                continue
            chosen = QPoint(x, y)
            break
        if chosen is None:
            x = max(s.left() + 2, min(anchor.x(), max(s.left() + 2, s.right() - pw - 2)))
            y = max(s.top() + 2, min(anchor.y() + gap + 8, max(s.top() + 2, s.bottom() - ph - 2)))
            chosen = QPoint(x, y)
        panel.move(chosen)

    def _on_text_preview_changed(self, text: str = "") -> None:
        """Refresh canvas WYSIWYG as user types or toggles style."""
        if self._text_edit is not None:
            self._text_preview = self._text_edit.text()
        else:
            self._text_preview = text or ""
        self.update()

    def _begin_text_input(self, pos: QPoint) -> None:
        """Visible in-place text field with real color controls (font / bg / outline)."""
        self._cancel_text_input()
        self._text_anchor = QPoint(pos)
        self._text_preview = ""

        panel = QWidget(self)
        panel.setObjectName("textPanel")
        panel.setStyleSheet(
            """
            QWidget#textPanel {
                background: rgba(15, 23, 42, 0.98);
                border: 2px solid #38bdf8;
                border-radius: 10px;
            }
            QPushButton {
                background: #0ea5e9; color: white; border: none; border-radius: 6px;
                padding: 6px 10px; font-weight: 800; font-size: 12px;
            }
            QPushButton#soft { background: #334155; color: #f1f5f9; }
            QCheckBox { color: #e2e8f0; font-size: 12px; spacing: 6px; }
            QLabel#hint { color: #94a3b8; font-size: 11px; }
            """
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        edit = QLineEdit(panel)
        edit.setPlaceholderText("在此输入文字…（选区上会实时预览）")
        edit.setMinimumWidth(260)
        edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        edit.setClearButtonEnabled(True)
        edit.returnPressed.connect(self._commit_text_input)
        edit.textChanged.connect(self._on_text_preview_changed)
        lay.addWidget(edit)

        # Row: font color
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("字体颜色", objectName="hint"))
        self._sw_font = QLabel()
        self._sw_font.setFixedSize(22, 22)
        btn_font = QPushButton("选颜色", objectName="soft")
        btn_font.setToolTip("文字本身的颜色（也可用下方色板）")
        btn_font.clicked.connect(self._pick_text_font_color)
        r1.addWidget(self._sw_font)
        r1.addWidget(btn_font)
        r1.addStretch(1)
        lay.addLayout(r1)

        # Row: background
        r2 = QHBoxLayout()
        chk_bg = QCheckBox("启用背景")
        chk_bg.setChecked(bool(self.text_bg))
        chk_bg.setToolTip("文字后面加色块，杂乱截图上也看得清")
        chk_bg.toggled.connect(self._on_text_bg_toggled)
        self._sw_bg = QLabel()
        self._sw_bg.setFixedSize(22, 22)
        btn_bg = QPushButton("背景色", objectName="soft")
        btn_bg.setToolTip("选择背景色（支持透明度）")
        btn_bg.clicked.connect(self._pick_text_bg_color)
        r2.addWidget(chk_bg)
        r2.addWidget(self._sw_bg)
        r2.addWidget(btn_bg)
        r2.addStretch(1)
        lay.addLayout(r2)

        # Row: outline
        r3 = QHBoxLayout()
        chk_ol = QCheckBox("启用描边")
        chk_ol.setChecked(bool(self.text_outline))
        chk_ol.setToolTip("文字周围描边，提高对比度")
        chk_ol.toggled.connect(self._on_text_outline_toggled)
        self._sw_ol = QLabel()
        self._sw_ol.setFixedSize(22, 22)
        btn_ol = QPushButton("描边色", objectName="soft")
        btn_ol.setToolTip("选择描边颜色")
        btn_ol.clicked.connect(self._pick_text_outline_color)
        r3.addWidget(chk_ol)
        r3.addWidget(self._sw_ol)
        r3.addWidget(btn_ol)
        r3.addStretch(1)
        lay.addLayout(r3)

        tip = QLabel("点击处实时预览最终效果 · 字号=粗细 · 确认写入", objectName="hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QHBoxLayout()
        btn_ok = QPushButton("确认")
        btn_ok.setToolTip("把文字画到截图上（不会退出截图）")
        btn_ok.clicked.connect(self._commit_text_input)
        btn_cancel = QPushButton("取消", objectName="soft")
        btn_cancel.clicked.connect(self._cancel_text_input)
        row.addWidget(btn_ok)
        row.addWidget(btn_cancel)
        row.addStretch(1)
        lay.addLayout(row)

        self._text_panel = panel
        self._text_edit = edit
        self._chk_text_bg = chk_bg
        self._chk_text_ol = chk_ol
        self._refresh_text_swatches()

        panel.adjustSize()
        pw = max(panel.sizeHint().width(), 340)
        ph = max(panel.sizeHint().height(), 200)
        panel.resize(pw, ph)
        self._place_text_panel(panel, pos, pw, ph)
        panel.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        panel.show()
        panel.raise_()
        # Tool overlay often keeps focus on the host — force line edit to receive keys/IME
        self._set_action_shortcuts_enabled(False)
        self.activateWindow()
        panel.activateWindow()
        edit.setFocus(Qt.FocusReason.OtherFocusReason)
        QTimer.singleShot(0, lambda e=edit: self._focus_text_edit(e))
        QTimer.singleShot(50, lambda e=edit: self._focus_text_edit(e))

        self._status_hint = "文字实时预览中 · 改字/改色立刻看到 · 确认写入"
        self.update()

    def _focus_text_edit(self, edit: QLineEdit | None) -> None:
        """Re-focus the text field after panel/color UI settles (Tool window focus quirks)."""
        if edit is None or self._text_edit is not edit:
            return
        try:
            if not edit.isVisible():
                return
            self.activateWindow()
            QApplication.setActiveWindow(self)
            edit.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception:
            pass

    def _on_text_bg_toggled(self, on: bool) -> None:
        self.text_bg = bool(on)
        self._save_annot_prefs()
        self._status_hint = "文字背景：开" if on else "文字背景：关"
        self._on_text_preview_changed()

    def _on_text_outline_toggled(self, on: bool) -> None:
        self.text_outline = bool(on)
        self._save_annot_prefs()
        self._status_hint = "文字描边：开" if on else "文字描边：关"
        self._on_text_preview_changed()

    def _pick_text_font_color(self) -> None:
        c = self._pick_color_dialog(self.color, "文字颜色", alpha=False)
        if c is not None:
            self._set_color(c)
            self._refresh_text_swatches()
            self._status_hint = f"字体颜色 {c.name()}（已记住）"
            self._on_text_preview_changed()
        QTimer.singleShot(0, lambda: self._focus_text_edit(self._text_edit))

    def _pick_text_bg_color(self) -> None:
        c = self._pick_color_dialog(self.text_bg_color, "文字背景色", alpha=True)
        if c is not None:
            if c.alpha() < 40:
                c.setAlpha(210)
            self.text_bg_color = c
            self.text_bg = True
            if getattr(self, "_chk_text_bg", None) is not None:
                self._chk_text_bg.blockSignals(True)
                self._chk_text_bg.setChecked(True)
                self._chk_text_bg.blockSignals(False)
            self._save_annot_prefs()
            self._refresh_text_swatches()
            self._status_hint = f"背景色已设置（透明度 {c.alpha()}）"
            self._on_text_preview_changed()
        QTimer.singleShot(0, lambda: self._focus_text_edit(self._text_edit))

    def _pick_text_outline_color(self) -> None:
        c = self._pick_color_dialog(self.text_outline_color, "文字描边色", alpha=False)
        if c is not None:
            self.text_outline_color = c
            self.text_outline = True
            if getattr(self, "_chk_text_ol", None) is not None:
                self._chk_text_ol.blockSignals(True)
                self._chk_text_ol.setChecked(True)
                self._chk_text_ol.blockSignals(False)
            self._save_annot_prefs()
            self._refresh_text_swatches()
            self._status_hint = f"描边色 {c.name()}（已记住）"
            self._on_text_preview_changed()
        QTimer.singleShot(0, lambda: self._focus_text_edit(self._text_edit))

    def _destroy_text_ui(self) -> None:
        if self._text_panel is not None:
            self._text_panel.hide()
            self._text_panel.deleteLater()
            self._text_panel = None
        elif self._text_edit is not None:
            self._text_edit.hide()
            self._text_edit.deleteLater()
        self._text_edit = None
        self._text_preview = ""
        self._sw_font = None
        self._sw_bg = None
        self._sw_ol = None
        self._chk_text_bg = None
        self._chk_text_ol = None
        self._set_action_shortcuts_enabled(True)

    def _commit_text_input(self) -> None:
        edit = self._text_edit
        if edit is None and self._text_panel is None:
            return
        text = (edit.text().strip() if edit is not None else "")
        pos = QPoint(self._text_anchor)
        self._destroy_text_ui()
        if text:
            bg = QColor(self.text_bg_color) if self.text_bg else None
            if bg is not None and bg.alpha() < 40:
                bg.setAlpha(210)
            ol_c = QColor(self.text_outline_color) if self.text_outline else None
            st = Stroke(
                kind="text",
                points=[QPointF(pos)],
                color=QColor(self.color),
                width=float(self.pen_w),
                text=text,
                bg_color=bg,
                outline=bool(self.text_outline),
                outline_color=ol_c,
            )
            self.strokes.append(st)
            self.redo_stack.clear()
            flags = []
            if bg is not None:
                flags.append("背景")
            if self.text_outline:
                flags.append("描边")
            extra = ("+" + "+".join(flags)) if flags else "无底"
            self._status_hint = f"已添加文字「{text[:20]}」({extra}) · 共 {len(self.strokes)} 笔"
        else:
            self._status_hint = "未输入文字（已取消）"
        self.setFocus()
        self.update()

    def _cancel_text_input(self) -> None:
        if not self._text_input_active():
            return
        self._destroy_text_ui()
        self._status_hint = "已取消文字输入 · 可继续标注"
        self.setFocus()
        self.update()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.RightButton:
            if self._text_input_active():
                self._cancel_text_input()
                return
            if self.drawing:
                self.drawing = False
                self.cur_stroke = None
                self.update()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()

        # Click outside text panel commits (if has text) — panel geometry in parent coords
        if self._text_input_active() and self._text_panel is not None:
            if not self._text_panel.geometry().contains(pos):
                self._commit_text_input()
            else:
                return
        elif self._text_edit is not None:
            if not self._text_edit.geometry().contains(pos):
                self._commit_text_input()
            else:
                return

        # Clicks on emoji palette stay in the panel (child handles buttons)
        if self._emoji_panel is not None and self._emoji_panel.geometry().contains(pos):
            return

        # ---- SELECT PHASE: drag out a region ----
        if self.phase == "select":
            self.selecting = True
            self.sel_origin = pos
            self.sel = QRect(pos, pos)
            self.update()
            return

        # ---- EDIT: dock first (outside selection is OK for tools) ----
        hit = self._hit_dock(pos)
        if hit is not None:
            key, _icon, _tip, kind = hit
            self._handle_dock_click(key, kind)
            return

        s = self.sel.normalized()
        if s.isNull() or s.width() < 4:
            return
        if not s.contains(pos):
            self._status_hint = "请点选区周边工具，或在蓝框内绘制"
            self.update()
            return

        pos = self._clamp_to_sel(pos)
        self.drawing = True
        self.redo_stack.clear()

        if self.tool == "text":
            self.drawing = False
            self._begin_text_input(pos)
            return

        if self.tool == "number":
            num = self._next_number()
            st = Stroke(
                kind="number",
                points=[QPointF(pos)],
                color=QColor(self.color),
                width=float(self.pen_w),
                number=num,
            )
            self.strokes.append(st)
            self.drawing = False
            self._status_hint = f"已添加序号 {st.number} · 共 {len(self.strokes)} 笔"
            self.update()
            return

        if self.tool == "emoji":
            ch = (self._emoji_char or "😀").strip() or "😀"
            st = Stroke(
                kind="emoji",
                points=[QPointF(pos)],
                color=QColor(self.color),
                width=float(self.pen_w),
                text=ch,
            )
            self.strokes.append(st)
            self.drawing = False
            self.redo_stack.clear()
            self._status_hint = f"已添加表情 {ch} · 共 {len(self.strokes)} 笔 · 滚轮调大小"
            self.update()
            return

        self.cur_stroke = Stroke(
            kind=self.tool,
            points=[QPointF(pos)],
            color=QColor(self.color),
            width=float(self.pen_w),
        )
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        pos = e.position().toPoint()
        self.hover = pos
        if self.phase == "select":
            if self.selecting:
                self.sel = QRect(self.sel_origin, pos).normalized()
            self.update()
            return
        if self.phase == "edit":
            hit = self._hit_dock(pos)
            new_key = hit[0] if hit else ""
            if new_key != self._hover_dock_key:
                self._hover_dock_key = new_key
                if not self.drawing:
                    self.update()
            if self.drawing and self.cur_stroke:
                pos = self._clamp_to_sel(pos)
                if self.cur_stroke.kind in ("pen", "marker"):
                    self._append_freehand_point(self.cur_stroke, QPointF(pos))
                else:
                    if len(self.cur_stroke.points) == 1:
                        self.cur_stroke.points.append(QPointF(pos))
                    else:
                        self.cur_stroke.points[1] = QPointF(pos)
                self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.phase == "select" and self.selecting:
            self.selecting = False
            self.sel = self.sel.normalized()
            if self.sel.width() < 4 or self.sel.height() < 4:
                self.sel = QRect()
                self.phase = "select"
            else:
                self._enter_edit_mode()
            self.update()
            return
        if self.phase == "edit" and self.drawing and self.cur_stroke:
            st = self.cur_stroke
            self.cur_stroke = None
            self.drawing = False
            if st.kind in ("pen", "marker"):
                end = self._clamp_to_sel(e.position().toPoint())
                self._append_freehand_point(st, QPointF(end), min_dist=0.5)
            if st.kind in ("pixelate", "blur") and len(st.points) >= 2:
                self._bake_region_effect(st)
            if st.kind in ("pen", "marker") and len(st.points) < 1:
                return
            if st.kind not in ("pen", "marker", "text", "number") and len(st.points) < 2:
                return
            self.strokes.append(st)
            self._status_hint = f"已添加 · 共 {len(self.strokes)} 笔 · 可继续换工具"
            self.update()
            return

    def _bake_region_effect(self, st: Stroke) -> None:
        r = QRectF(st.points[0], st.points[1]).normalized().toRect()
        r = r.intersected(self.rect())
        if r.width() < 2 or r.height() < 2:
            return
        # base from background + already baked strokes drawn... approximate: from bg only then overlay prior
        # Better: render composite crop
        composite = self._render_full_composite()
        crop = composite.copy(r)
        if Image is None:
            # simple pixelate with Qt
            if st.kind == "pixelate":
                scale = max(2, int(self.pen_w))
                small = crop.scaled(
                    max(1, r.width() // scale),
                    max(1, r.height() // scale),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                )
                st.baked = small.scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
            else:
                st.baked = crop.scaled(
                    max(1, r.width() // 8),
                    max(1, r.height() // 8),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ).scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            return
        try:
            from PIL.ImageQt import fromqimage as pil_from_qimage, ImageQt as PilImageQt

            pil = pil_from_qimage(crop)
        except Exception:
            # Fallback: Qt soft scale
            st.baked = crop.scaled(
                max(1, r.width() // 8),
                max(1, r.height() // 8),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ).scaled(r.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            return
        if st.kind == "pixelate":
            block = max(4, int(self.pen_w * 2))
            small = pil.resize((max(1, pil.width // block), max(1, pil.height // block)), Image.Resampling.NEAREST)
            pil = small.resize(pil.size, Image.Resampling.NEAREST)
        else:
            rad = max(2, int(self.pen_w))
            pil = pil.filter(ImageFilter.GaussianBlur(radius=rad))
        try:
            st.baked = pil.toqimage()
        except Exception:
            st.baked = PilImageQt(pil)

    def _render_full_composite(self) -> QImage:
        img = self.bg.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for st in self.strokes:
            self._paint_stroke(p, st)
        p.end()
        return img

    def export_image(self) -> QImage | None:
        s = self.sel.normalized()
        if s.width() < 2 or s.height() < 2:
            return None
        full = self._render_full_composite()
        return full.copy(s)

    def _default_save_dir(self) -> Path:
        d = self.cfg.get("save_dir") or str(Path.home() / "Pictures" / "ParrotScreenshots")
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _append_freehand_point(stroke: Stroke, pt: QPointF, min_dist: float = 2.5) -> None:
        pts = stroke.points
        if not pts:
            pts.append(pt)
            return
        last = pts[-1]
        dx = pt.x() - last.x()
        dy = pt.y() - last.y()
        if dx * dx + dy * dy < min_dist * min_dist:
            return
        pts.append(pt)

    @staticmethod
    def _smooth_stroke_path(points: list[QPointF]) -> QPainterPath:
        if not points:
            return QPainterPath()
        if len(points) == 1:
            return QPainterPath(points[0])
        if len(points) == 2:
            path = QPainterPath(points[0])
            path.lineTo(points[1])
            return path
        path = QPainterPath(points[0])
        for i in range(1, len(points) - 1):
            mid = QPointF(
                (points[i].x() + points[i + 1].x()) * 0.5,
                (points[i].y() + points[i + 1].y()) * 0.5,
            )
            path.quadTo(points[i], mid)
        path.lineTo(points[-1])
        return path

    def _finish_ok(self, img: QImage) -> None:
        self.finished.emit(img)
        self.close()

    def _on_action(self, act: str) -> None:
        if act == "cancel":
            self.finished.emit(None)
            self.close()
            return
        if act == "reselect":
            # Explicit only — never happens by accident while annotating
            if self.strokes:
                reply = QMessageBox.question(
                    self,
                    "重新框选",
                    "重新框选会清空当前标注，确定吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self._enter_select_mode(clear_strokes=True)
            return
        if act == "undo":
            if self.strokes:
                self.redo_stack.append(self.strokes.pop())
                # Keep number stamps consecutive: next id = max remaining + 1
                nxt = self._next_number()
                self._status_hint = f"已撤销 · 剩余 {len(self.strokes)} 笔 · 下一序号 {nxt}"
                self.update()
            return
        if act == "redo":
            if self.redo_stack:
                self.strokes.append(self.redo_stack.pop())
                nxt = self._next_number()
                self._status_hint = f"已重做 · 共 {len(self.strokes)} 笔 · 下一序号 {nxt}"
                self.update()
            return
        img = self.export_image()
        if img is None or img.isNull():
            if act in ("copy", "save", "upload", "pin", "accept"):
                QMessageBox.information(self, "截图", "请先框选有效区域")
            return
        if act == "copy":
            QApplication.clipboard().setImage(img)
            self._finish_ok(img)
            return
        if act == "save":
            path = self._default_save_dir() / f"shot_{time.strftime('%Y%m%d_%H%M%S')}.png"
            chosen, _ = QFileDialog.getSaveFileName(
                self, "保存截图", str(path), "PNG (*.png);;JPEG (*.jpg)"
            )
            if chosen:
                img.save(chosen)
                self.cfg["last_save"] = chosen
                self._finish_ok(img)
            return
        if act == "pin":
            pm = QPixmap.fromImage(img)
            pin = PinnedShot(pm)
            pin.move(self.desk_geo.x() + self.sel.x() + 20, self.desk_geo.y() + self.sel.y() + 20)
            pin.show()
            self._pinned.append(pin)
            self._finish_ok(img)
            return
        if act == "upload":
            if not self.on_upload:
                why = getattr(self, "_upload_block_reason", "") or "未配置云端：请在截图设置里选择密钥并点「连接 Google」"
                self._status_hint = why
                self.update()
                return
            tmp = Path(tempfile.gettempdir()) / f"parrot_shot_{int(time.time())}.png"
            img.save(str(tmp), "PNG")
            try:
                result = self.on_upload(tmp)
                link, name = _parse_upload_result(result)
                self._last_upload_link = link
                if link:
                    QApplication.clipboard().setText(link)
                self._finish_ok(img)
            except Exception as e:
                self._status_hint = f"上传失败：{e}"
                self.update()
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
            return
        if act == "accept":
            # auto save + copy image then exit
            path = self._default_save_dir() / f"shot_{time.strftime('%Y%m%d_%H%M%S')}.png"
            img.save(str(path))
            QApplication.clipboard().setImage(img)
            auto = bool(self.cfg.get("auto_upload"))
            if auto and self.on_upload:
                try:
                    result = self.on_upload(path)
                    link, name = _parse_upload_result(result)
                    if link:
                        QApplication.clipboard().setText(link)
                        self._last_upload_link = link
                except Exception:
                    pass
            self._finish_ok(img)
            return

    def closeEvent(self, e) -> None:
        super().closeEvent(e)


# Keep pinned windows alive
_PINNED_REFS: list[PinnedShot] = []
_EDITOR_REF: ScreenshotEditor | None = None
_SHOT_TOKEN = 0  # coalesce rapid hotkeys: only the latest request runs


def _parse_upload_result(result) -> tuple[str, str]:
    """Normalize upload callback return → (link, name)."""
    if isinstance(result, dict):
        link = str(result.get("link") or result.get("webViewLink") or "").strip()
        name = str(result.get("name") or "").strip()
        fid = str(result.get("id") or "").strip()
        if not link and fid:
            link = f"https://drive.google.com/file/d/{fid}/view"
        return link, name
    text = str(result or "").strip()
    if not text:
        return "", ""
    # "name\nlink" legacy format
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    link = ""
    name = lines[0] if lines else ""
    for ln in lines:
        try:
            from urllib.parse import urlparse

            pu = urlparse(ln)
            if (pu.scheme or "").lower() in {"http", "https"} and (pu.hostname or ""):
                link = ln
                break
        except Exception:
            continue
    if link and name == link:
        name = "截图"
    return link, name


def show_upload_link_dialog(
    parent,
    *,
    name: str = "",
    link: str = "",
    auto_exit_hint: bool = False,
) -> None:
    """Show image URL after cloud upload; auto-copy + one-click copy again."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("上传成功 · 图片链接")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dlg.setMinimumWidth(480)
    dlg.setStyleSheet(
        """
        QDialog { background: #0f172a; }
        QLabel { color: #e2e8f0; font-size: 12px; }
        QLabel#title { color: #38bdf8; font-size: 15px; font-weight: 800; }
        QLineEdit {
            background: #020617; color: #7dd3fc; border: 1px solid #334155;
            border-radius: 8px; padding: 8px; font-size: 12px;
        }
        QPushButton {
            background: #0ea5e9; color: white; border: none; border-radius: 8px;
            padding: 8px 14px; font-weight: 800;
        }
        QPushButton#soft { background: #1e293b; border: 1px solid #334155; }
        """
    )
    lay = QVBoxLayout(dlg)
    lay.setSpacing(10)
    title = QLabel("✅ 已上传到 Google 云端硬盘")
    title.setObjectName("title")
    lay.addWidget(title)
    if name:
        lay.addWidget(QLabel(f"文件名：{name}"))
    if link:
        lay.addWidget(QLabel("图片链接（已自动复制到剪贴板）："))
        row = QHBoxLayout()
        edit = QLineEdit(link)
        edit.setReadOnly(True)
        edit.selectAll()
        row.addWidget(edit, 1)

        def _copy() -> None:
            QApplication.clipboard().setText(link)
            tip.setText("链接已复制到剪贴板 ✓")

        btn_copy = QPushButton("复制链接")
        btn_copy.clicked.connect(_copy)
        row.addWidget(btn_copy)
        lay.addLayout(row)
        tip = QLabel("可直接粘贴分享。编辑器仍保持打开，可继续标注。")
        if auto_exit_hint:
            tip.setText("链接已复制。关闭此窗口后截图将结束。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#94a3b8;")
        lay.addWidget(tip)
        # Ensure clipboard has the link
        QApplication.clipboard().setText(link)
    else:
        lay.addWidget(QLabel("上传成功，但未返回可分享链接（请检查 Drive 权限/分享设置）。"))
    btns = QHBoxLayout()
    if link:
        btn_open = QPushButton("浏览器打开", objectName="soft")

        def _open() -> None:
            import webbrowser

            webbrowser.open(link)

        btn_open.clicked.connect(_open)
        btns.addWidget(btn_open)
    btns.addStretch(1)
    btn_ok = QPushButton("好的")
    btn_ok.clicked.connect(dlg.accept)
    btns.addWidget(btn_ok)
    lay.addLayout(btns)
    dlg.exec()


def start_screenshot(
    *,
    mode: str = "region",
    state: dict | None = None,
    on_done: Callable[[QImage | None], None] | None = None,
) -> ScreenshotEditor | None:
    """Launch capture+editor. mode: region | full.

    Rapid hotkey presses coalesce: only the *latest* request runs (previous
    pending timers and an unfinished editor are discarded).
    """
    global _EDITOR_REF, _SHOT_TOKEN
    cfg = {}
    if isinstance(state, dict):
        cfg = state.setdefault("screenshot", {})

    app = QApplication.instance()
    if app is None:
        return None

    # Invalidate any earlier queued launches
    _SHOT_TOKEN += 1
    token = _SHOT_TOKEN

    # Close an existing editor still open (avoids stacking / freeze)
    prev = _EDITOR_REF
    if prev is not None:
        try:
            prev.close()
        except Exception:
            pass
        try:
            prev.deleteLater()
        except Exception:
            pass
        _EDITOR_REF = None

    def _run() -> None:
        global _EDITOR_REF
        # Superseded by a newer hotkey press
        if token != _SHOT_TOKEN:
            return
        try:
            bg, geo = capture_virtual_desktop()
        except Exception as e:
            if token == _SHOT_TOKEN:
                QMessageBox.warning(None, "截图失败", str(e))
                if on_done:
                    try:
                        on_done(None)
                    except Exception:
                        pass
            return
        if token != _SHOT_TOKEN:
            return

        def do_upload(path: Path) -> dict:
            from gdrive_client import GoogleDriveClient

            gcfg = dict(cfg.get("gdrive") or {})
            client = GoogleDriveClient(gcfg)
            result = client.upload_file(path)
            cfg.setdefault("gdrive", {}).update(gcfg)
            if state is not None and hasattr(state, "get"):
                state.setdefault("screenshot", cfg)
            fid = str(result.get("id") or "")
            link = str(result.get("webViewLink") or "").strip()
            if not link and fid:
                link = f"https://drive.google.com/file/d/{fid}/view"
            return {
                "id": fid,
                "name": str(result.get("name") or path.name),
                "link": link,
                "webViewLink": link,
            }

        on_upload = None
        g = cfg.get("gdrive") or {}
        upload_block_reason = ""
        if g.get("enabled"):
            try:
                from gdrive_client import GoogleDriveClient

                client = GoogleDriveClient(g)
                secrets = str(g.get("client_secrets_path") or "").strip()
                if not secrets:
                    upload_block_reason = "已启用云端但未选择 OAuth JSON"
                elif not Path(secrets).is_file():
                    upload_block_reason = f"找不到 OAuth JSON: {secrets}"
                elif not client.is_connected():
                    upload_block_reason = "已选密钥但未连接 Google（请在截图设置点「连接 Google」）"
                else:
                    on_upload = do_upload
            except Exception as exc:
                on_upload = None
                upload_block_reason = f"云端初始化失败: {exc}"
        elif g.get("client_secrets_path"):
            # Secrets present but upload disabled — soft hint only when user hits upload
            upload_block_reason = "云端未启用：请在截图设置勾选「启用 Google 云端上传」并连接"

        if token != _SHOT_TOKEN:
            return

        ed = ScreenshotEditor(bg, geo, mode=mode, cfg=cfg, on_upload=on_upload)
        if upload_block_reason and not on_upload:
            ed._upload_block_reason = upload_block_reason
        _EDITOR_REF = ed

        def _fin(img):
            global _EDITOR_REF
            if on_done:
                on_done(img)
            # keep pins
            for pin in ed._pinned:
                _PINNED_REFS.append(pin)
            if _EDITOR_REF is ed:
                _EDITOR_REF = None

        ed.finished.connect(_fin)
        ed.destroyed.connect(lambda *_: _clear_editor_ref(ed))
        ed.show()
        ed.raise_()
        ed.activateWindow()
        ed.setFocus()

    # brief delay so menus close; token ensures only latest hotkey wins
    QTimer.singleShot(80, _run)
    return None


def _clear_editor_ref(ed) -> None:
    global _EDITOR_REF
    if _EDITOR_REF is ed:
        _EDITOR_REF = None
