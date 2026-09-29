"""Floating screen recorder board with live preview."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QPoint, QRect, QTimer, pyqtSignal, QObject, QSize

try:
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore

    HAS_WIN32 = True
except ImportError:
    win32api = None  # type: ignore
    win32con = None  # type: ignore
    win32gui = None  # type: ignore
    HAS_WIN32 = False
from PyQt6.QtGui import QPainter, QPen, QColor, QImage, QPixmap, QMouseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import screen_recorder


class _PreviewBridge(QObject):
    frame = pyqtSignal(object)  # numpy BGR
    status = pyqtSignal(str)
    finished = pyqtSignal(str)
    mic_level = pyqtSignal(float)  # 0..100


class RecordingDrawOverlay(QWidget):
    """Draw layer over the capture region.

    Windows + WA_TranslucentBackground only delivers mouse events on *opaque*
    pixels, so a fully transparent overlay never receives strokes. In draw mode
    we paint a faint veil (non-zero alpha) so every drag is captured.
    """

    exit_requested = pyqtSignal()  # user closed draw mode from overlay UI

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.drawing = False
        self.last_point = QPoint()
        self.cursor_pos = QPoint(-100, -100)
        self.brush_color = QColor(239, 68, 68)
        self.brush_size = 8
        self.canvas_image: QImage | None = None
        self.overlay_rgba: np.ndarray | None = None
        self.is_draw_mode = False
        self.target_region: dict | None = None
        self._exit_btn = QRect()  # close-draw button on overlay

        self.track_timer = QTimer(self)
        self.track_timer.timeout.connect(self._track_region)
        # 250ms is enough to follow moving windows; 100ms burned CPU while drawing
        self.track_timer.start(250)
        # After winId exists, exclude this overlay from OS screen capture (strokes
        # are composited in software via overlay_rgba — avoids green/gray veil in video).
        QTimer.singleShot(0, self._exclude_from_capture)

    def _exclude_from_capture(self) -> None:
        """Windows 10 2004+: do not include this HWND in mss/DXGI screen capture."""
        if not HAS_WIN32:
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    def _make_pen_cursor(self) -> None:
        from PyQt6.QtGui import QCursor, QPixmap, QPainterPath

        size = 48
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(30, 30, 30), 2))
        p.setBrush(self.brush_color)
        path = QPainterPath()
        path.moveTo(8, 40)
        path.lineTo(14, 28)
        path.lineTo(34, 8)
        path.lineTo(40, 14)
        path.lineTo(20, 34)
        path.closeSubpath()
        p.drawPath(path)
        p.setBrush(self.brush_color)
        p.drawEllipse(4, 38, 10, 10)
        p.setPen(QPen(QColor(255, 255, 255), 2))
        p.drawEllipse(4, 38, 10, 10)
        p.setPen(QPen(self.brush_color, 2, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = max(6, min(16, self.brush_size))
        p.drawEllipse(6 - r // 2, 40 - r // 2, r * 2, r * 2)
        p.end()
        self.setCursor(QCursor(pm, 8, 42))

    def _force_topmost_clickable(self) -> None:
        """Windows: topmost + clear WS_EX_TRANSPARENT so clicks hit us."""
        if not HAS_WIN32:
            return
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOACTIVATE = 0x08000000
            ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
            ex |= WS_EX_LAYERED
            ex &= ~WS_EX_TRANSPARENT
            ex &= ~WS_EX_NOACTIVATE
            win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, ex)
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
                | win32con.SWP_FRAMECHANGED,
            )
        except Exception:
            pass

    def set_region(self, region: dict | None) -> None:
        self.target_region = dict(region) if region else None
        self._track_region()

    def set_draw_mode(self, enabled: bool) -> None:
        self.is_draw_mode = bool(enabled)
        if enabled:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self._track_region()
            self._ensure_canvas()
            self._make_pen_cursor()
            self.show()
            self.raise_()
            self.activateWindow()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self._force_topmost_clickable()
            self._exclude_from_capture()
            self.update()
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.drawing = False
            self.cursor_pos = QPoint(-100, -100)
            # Keep overlay visible (strokes) but click-through while not drawing
            self.update()

    def _ensure_canvas(self) -> None:
        w = max(2, self.width())
        h = max(2, self.height())
        need = (
            self.canvas_image is None
            or self.canvas_image.isNull()
            or self.canvas_image.width() != w
            or self.canvas_image.height() != h
        )
        if not need:
            return
        # Explicit transparent canvas (avoid premultiplied gray/green fringes)
        new_img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        new_img.fill(0)  # fully transparent
        if self.canvas_image is not None and not self.canvas_image.isNull():
            p = QPainter(new_img)
            p.drawImage(0, 0, self.canvas_image)
            p.end()
        self.canvas_image = new_img
        self._sync_rgba()

    def _track_region(self) -> None:
        r = self.target_region
        if not r:
            return
        try:
            hwnd = int(r.get("hwnd") or 0)
            if HAS_WIN32 and hwnd > 0 and win32gui.IsWindow(hwnd):
                rect = win32gui.GetWindowRect(hwnd)
                x, y = rect[0], rect[1]
                w, h = max(2, rect[2] - rect[0]), max(2, rect[3] - rect[1])
                self.target_region = {**r, "left": x, "top": y, "width": w, "height": h}
            else:
                x = int(r.get("left") or 0)
                y = int(r.get("top") or 0)
                w = max(2, int(r.get("width") or 100))
                h = max(2, int(r.get("height") or 100))
            geo = self.geometry()
            if geo.x() != x or geo.y() != y or geo.width() != w or geo.height() != h:
                self.setGeometry(x, y, w, h)
            self._ensure_canvas()
            if self.is_draw_mode:
                self._force_topmost_clickable()
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._ensure_canvas()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Windows hit-tests only non-zero alpha pixels. Use alpha=1 (invisible to eye)
        # so mss recordings no longer bake in a cyan veil.
        if self.is_draw_mode:
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))
            # Compact exit chip only — no cyan border / banner (those get recorded)
            bw, bh = 96, 28
            self._exit_btn = QRect(max(8, self.width() - bw - 10), 8, bw, bh)
            p.setBrush(QColor(220, 38, 38, 210))
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawRoundedRect(self._exit_btn, 7, 7)
            p.setPen(QColor(255, 255, 255))
            p.drawText(self._exit_btn, Qt.AlignmentFlag.AlignCenter, "关闭画笔")
        else:
            self._exit_btn = QRect()
        if self.canvas_image is not None and not self.canvas_image.isNull():
            p.drawImage(0, 0, self.canvas_image)
        if self.is_draw_mode and self.cursor_pos.x() >= 0:
            r = max(4, self.brush_size // 2 + 2)
            cx, cy = self.cursor_pos.x(), self.cursor_pos.y()
            p.setPen(QPen(QColor(255, 255, 255, 230), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPoint(cx, cy), r + 2, r + 2)
            col = QColor(self.brush_color)
            col.setAlpha(200)
            p.setPen(QPen(col, 2))
            p.setBrush(col)
            p.drawEllipse(QPoint(cx, cy), max(3, self.brush_size // 2), max(3, self.brush_size // 2))

    def _map_pos(self, event: QMouseEvent) -> QPoint:
        try:
            gp = event.globalPosition().toPoint()
            return self.mapFromGlobal(gp)
        except Exception:
            return event.position().toPoint()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.is_draw_mode:
            return
        pt = self._map_pos(event)
        self.cursor_pos = pt
        # Close button / right-click exits draw mode
        if event.button() == Qt.MouseButton.RightButton:
            self.exit_requested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._exit_btn.contains(pt):
            self.exit_requested.emit()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._ensure_canvas()
        if self.canvas_image is None:
            return
        self.drawing = True
        self.last_point = pt
        p = QPainter(self.canvas_image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self.brush_color)
        r = max(2, self.brush_size // 2)
        p.drawEllipse(pt, r, r)
        p.end()
        self._sync_rgba()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pt = self._map_pos(event)
        self.cursor_pos = pt
        if self.drawing and self.is_draw_mode:
            self._ensure_canvas()
            if self.canvas_image is not None:
                p = QPainter(self.canvas_image)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(
                    QPen(
                        self.brush_color,
                        max(1, self.brush_size),
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
                p.drawLine(self.last_point, pt)
                p.end()
                self.last_point = pt
                self._sync_rgba()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drawing = False
            self._sync_rgba()
            self.update()
            event.accept()

    def clear_canvas(self) -> None:
        if self.canvas_image is not None:
            self.canvas_image.fill(Qt.GlobalColor.transparent)
            self.update()
            self._sync_rgba()

    def _sync_rgba(self) -> None:
        if self.canvas_image is None or self.canvas_image.isNull():
            self.overlay_rgba = None
            return
        try:
            img = self.canvas_image.convertToFormat(QImage.Format.Format_RGBA8888)
            w, h = img.width(), img.height()
            bpl = img.bytesPerLine()
            ptr = img.bits()
            ptr.setsize(img.sizeInBytes())
            raw = np.frombuffer(ptr, np.uint8).reshape((h, bpl))[:, : w * 4].copy()
            self.overlay_rgba = raw.reshape((h, w, 4))
        except Exception:
            try:
                img = self.canvas_image.convertToFormat(QImage.Format.Format_RGBA8888)
                w, h = img.width(), img.height()
                ptr = img.bits()
                ptr.setsize(img.sizeInBytes())
                self.overlay_rgba = np.frombuffer(ptr, np.uint8).reshape((h, w, 4)).copy()
            except Exception:
                self.overlay_rgba = None

    @property
    def overlay_pil(self):
        if self.overlay_rgba is None:
            return None
        try:
            from PIL import Image

            return Image.fromarray(self.overlay_rgba, "RGBA")
        except Exception:
            return None


class RecordingControlBar(QWidget):
    """Floating bar while recording: brush / cursor / pause / stop."""

    def __init__(self, board: "FloatingRecorderBoard", parent=None):
        super().__init__(parent)
        self.board = board
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            """
            QFrame#bar {
                background: rgba(15,23,42,0.96); border: 1px solid #6366f1;
                border-radius: 14px;
            }
            QLabel { color: #e2e8f0; font-weight: 700; }
            QPushButton {
                background: #6366f1; color: white; border: none; border-radius: 8px;
                padding: 8px 12px; font-weight: 800;
            }
            QPushButton#soft { background: #1e293b; border: 1px solid #334155; }
            QPushButton#soft:checked { background: #0ea5e9; color: #0f172a; }
            QPushButton#danger { background: #dc2626; }
            """
        )
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        bar = QFrame(objectName="bar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self.lbl = QLabel("🔴 录制中")
        lay.addWidget(self.lbl)
        self.btn_cursor = QPushButton("指针色", objectName="soft")
        self.btn_cursor.clicked.connect(board._pick_cursor_color)
        lay.addWidget(self.btn_cursor)
        self.btn_brush = QPushButton("画笔开", objectName="soft")
        self.btn_brush.setCheckable(True)
        self.btn_brush.setToolTip("再点一次关闭画笔")
        self.btn_brush.clicked.connect(self._sync_brush)
        lay.addWidget(self.btn_brush)
        self.cmb_brush = QComboBox()
        self.cmb_brush.addItems(["红色", "绿色", "黄色", "蓝色", "白色"])
        self.cmb_brush.setToolTip("画笔颜色（录制中可切换）")
        self.cmb_brush.setMinimumWidth(72)
        try:
            # Keep in sync with settings panel default
            cur = board.cmb_brush.currentText()
            idx = self.cmb_brush.findText(cur)
            if idx >= 0:
                self.cmb_brush.setCurrentIndex(idx)
        except Exception:
            pass
        self.cmb_brush.currentIndexChanged.connect(self._sync_brush_color)
        lay.addWidget(self.cmb_brush)
        self.btn_clear = QPushButton("清除笔画", objectName="soft")
        self.btn_clear.setToolTip("清除屏幕上已画内容（不取消录制）")
        self.btn_clear.clicked.connect(self._clear_strokes)
        lay.addWidget(self.btn_clear)
        self.btn_pause = QPushButton("暂停", objectName="soft")
        self.btn_pause.clicked.connect(board._pause_resume)
        lay.addWidget(self.btn_pause)
        self.btn_stop = QPushButton("停止保存", objectName="danger")
        self.btn_stop.clicked.connect(board._stop)
        lay.addWidget(self.btn_stop)
        root.addWidget(bar)
        self.adjustSize()
        self._expanded = False
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(self._auto_collapse)
        self.lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        QTimer.singleShot(0, self._exclude_from_capture)

    def _auto_collapse(self) -> None:
        """Hide full toolbar again while still recording (not paused)."""
        try:
            if not self.isVisible():
                return
            if self.board.recorder and getattr(self.board.recorder, "is_paused", False):
                return
            if self.board.recorder and getattr(self.board.recorder, "is_recording", False):
                self._expanded = False
                self._apply_collapsed_chrome(collapsed=True)
                self._exclude_from_capture()
                self._place_outside_capture()
        except Exception:
            pass

    def _exclude_from_capture(self) -> None:
        """Keep floating toolbar out of the recorded video (Windows)."""
        if not HAS_WIN32:
            return
        try:
            import ctypes

            hwnd = int(self.winId())
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass

    def _sync_brush(self) -> None:
        on = self.btn_brush.isChecked()
        self.board.btn_brush.setChecked(on)
        self.board._toggle_brush()
        self.btn_brush.setText("画笔关" if on else "画笔开")
        self._sync_brush_color()

    def _sync_brush_color(self) -> None:
        """Apply color from floating bar (works while settings panel is hidden)."""
        try:
            name = self.cmb_brush.currentText()
            # Sync settings combo without re-entry loops
            if hasattr(self.board, "cmb_brush"):
                self.board.cmb_brush.blockSignals(True)
                idx = self.board.cmb_brush.findText(name)
                if idx >= 0:
                    self.board.cmb_brush.setCurrentIndex(idx)
                self.board.cmb_brush.blockSignals(False)
            self.board._change_brush_color()
        except Exception:
            pass

    def _clear_strokes(self) -> None:
        self.board._clear_brush()
        # keep recording; only wipe overlay paint

    def set_recording_ui(self, recording: bool, paused: bool = False) -> None:
        """Show/hide recording controls.

        mss BitBlt often still captures WDA_EXCLUDEFROMCAPTURE windows, so during
        active recording we collapse to a tiny chip *outside* the capture region.
        Full toolbar shows when paused or when the chip is clicked.
        """
        if not recording:
            self._expanded = False
            self.hide()
            return
        self.lbl.setText("⏸ 已暂停" if paused else "🔴 录制中")
        self.btn_pause.setText("继续" if paused else "暂停")
        # Paused → always show full bar (user needs controls); recording → collapsed
        if paused:
            self._expanded = True
        self._apply_collapsed_chrome(collapsed=not self._expanded)
        self.show()
        self.raise_()
        self._exclude_from_capture()
        self._place_outside_capture()
        if self._expanded and not paused:
            # Auto-collapse so the bar doesn't linger in the video
            self._collapse_timer.start(3500)

    def _apply_collapsed_chrome(self, *, collapsed: bool) -> None:
        """Collapsed = only a small red chip; expanded = full toolbar."""
        widgets = [
            self.btn_cursor,
            self.btn_brush,
            self.cmb_brush,
            self.btn_clear,
            self.btn_pause,
            self.btn_stop,
        ]
        for w in widgets:
            w.setVisible(not collapsed)
        if collapsed:
            self.lbl.setText("🔴 点击展开工具条")
            self.lbl.setToolTip("录制中已隐藏工具条（避免进画面）。点击展开，暂停时也会自动展开。")
        else:
            self.lbl.setToolTip("录制控制条（已尽量放在录像区域外，并排除出系统捕获）")
        self.adjustSize()

    def _place_outside_capture(self) -> None:
        from PyQt6.QtGui import QGuiApplication

        scr = QGuiApplication.primaryScreen()
        if not scr:
            return
        g = scr.availableGeometry()
        self.adjustSize()
        x = g.center().x() - self.width() // 2
        y = g.bottom() - self.height() - 24
        try:
            reg = None
            if self.board.overlay and self.board.overlay.target_region:
                reg = self.board.overlay.target_region
            elif self.board.recorder:
                reg = screen_recorder.resolve_region(getattr(self.board.recorder.cfg, "target", None))
            if reg:
                rx = int(reg.get("left") or 0)
                ry = int(reg.get("top") or 0)
                rw = int(reg.get("width") or 0)
                rh = int(reg.get("height") or 0)
                x = rx + max(0, (rw - self.width()) // 2)
                below = ry + rh + 8
                if below + self.height() <= g.bottom() - 4:
                    y = below
                else:
                    above = ry - self.height() - 8
                    if above >= g.top() + 4:
                        y = above
                    else:
                        # Last resort: pin to screen corner outside typical capture
                        x = g.right() - self.width() - 12
                        y = g.top() + 12
        except Exception:
            pass
        self.move(x, y)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        # Click collapsed chip → expand full toolbar
        if e.button() == Qt.MouseButton.LeftButton and not self.btn_pause.isVisible():
            self._expanded = True
            self._apply_collapsed_chrome(collapsed=False)
            self._exclude_from_capture()
            self._place_outside_capture()
            self._collapse_timer.start(5000)
            e.accept()
            return
        super().mousePressEvent(e)

class FloatingRecorderBoard(QWidget):
    """Recorder settings (+ optional floating control bar while recording)."""

    def __init__(self, callbacks=None, state=None, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self.callbacks = callbacks
        self.state = state if isinstance(state, dict) else {}
        self.embedded = embedded
        self.recorder: screen_recorder.ScreenRecorder | None = None
        self.overlay: RecordingDrawOverlay | None = None
        self.dragging = False
        self.drag_position = QPoint()
        self._bridge = _PreviewBridge(self)
        self._bridge.frame.connect(self._on_preview_frame)
        self._bridge.status.connect(self._set_status)
        self._bridge.finished.connect(self._on_save_finished)
        self._bridge.mic_level.connect(self._on_mic_level)
        self._cursor_color = QColor(255, 220, 40)
        self._busy_stop = False
        self._mic_tester: screen_recorder.MicTester | None = None
        self.control_bar = RecordingControlBar(self)

        if not embedded:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(420, 620)
        else:
            self.setWindowFlags(Qt.WindowType.Widget)

        self.duration_timer = QTimer(self)
        self.duration_timer.timeout.connect(self._tick)
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._idle_preview)
        # Idle preview interval: macOS mss/CG is heavier — use slower tick
        import sys as _sys

        self.preview_timer.start(750 if _sys.platform == "darwin" else 500)

        self._loading_settings = True
        self._init_ui()
        self._load_settings()
        self._refresh_targets()
        self._refresh_audio()
        self._restore_selection()
        self._wire_settings_persist()
        self._loading_settings = False

    def _cfg(self) -> dict:
        return self.state.setdefault("recorder", {})

    def _init_ui(self) -> None:
        self.setStyleSheet(
            """
            QFrame#mainContainer {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                    stop:0 rgba(17,24,39,0.96), stop:1 rgba(11,18,32,0.98));
                border: 1px solid rgba(148,163,184,0.22);
                border-radius: 16px;
            }
            QLabel { color: #e2e8f0; font-size: 11px; font-weight: 600; }
            QLabel#title { color: #34d399; font-size: 14px; font-weight: 800; }
            QLabel#muted { color: #94a3b8; font-size: 10px; font-weight: 500; }
            QComboBox, QLineEdit, QSpinBox {
                background: rgba(15,23,42,0.85); color: #f8fafc;
                border: 1px solid rgba(148,163,184,0.22); border-radius: 8px;
                padding: 5px 8px; min-height: 26px;
            }
            QComboBox QAbstractItemView {
                background: #0f172a; color: #f8fafc;
                selection-background-color: #059669; border: 1px solid #334155;
            }
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #10b981, stop:1 #059669);
                border: none; color: white; font-weight: 700; font-size: 11px;
                padding: 7px 12px; border-radius: 9px;
            }
            QPushButton:hover { background: #34d399; color: #042f1a; }
            QPushButton:disabled { background: #334155; color: #94a3b8; }
            QPushButton#danger {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #ef4444, stop:1 #dc2626);
            }
            QPushButton#soft {
                background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.08);
            }
            QPushButton#soft:checked { background: rgba(52,211,153,0.25); border-color: #34d399; }
            QCheckBox { color: #cbd5e1; spacing: 6px; }
            QCheckBox::indicator {
                width: 15px; height: 15px; border-radius: 4px;
                border: 1px solid #64748b; background: #0f172a;
            }
            QCheckBox::indicator:checked { background: #10b981; border-color: #059669; }
            QSlider::groove:horizontal { height: 5px; background: #334155; border-radius: 3px; }
            QSlider::handle:horizontal {
                width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
                background: #fff; border: 2px solid #10b981;
            }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0 if self.embedded else 0, 0, 0, 0)
        box = QFrame(objectName="mainContainer")
        if self.embedded:
            box.setStyleSheet(
                "QFrame#mainContainer { background: transparent; border: none; }"
            )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4 if self.embedded else 14, 4 if self.embedded else 12, 4 if self.embedded else 14, 4 if self.embedded else 12)
        lay.setSpacing(8)

        header = QHBoxLayout()
        header.addWidget(QLabel("🎥 屏幕录制设置", objectName="title"), 1)
        if not self.embedded:
            close_btn = QPushButton("×")
            close_btn.setFixedSize(26, 26)
            close_btn.setObjectName("soft")
            close_btn.clicked.connect(self.hide)
            header.addWidget(close_btn)
        lay.addLayout(header)

        # Live preview
        self.preview_label = QLabel("预览：选择范围后显示实时画面")
        self.preview_label.setObjectName("muted")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(160)
        self.preview_label.setStyleSheet(
            "QLabel { background: #020617; border: 1px solid #1e293b; border-radius: 12px; color: #64748b; }"
        )
        lay.addWidget(self.preview_label)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.addWidget(QLabel("分辨率"), 0, 0)
        self.cmb_res = QComboBox()
        self.cmb_res.addItem("1080p 高清 (推荐)", "1080p")
        self.cmb_res.addItem("720p 流畅", "720p")
        self.cmb_res.addItem("1440p 2K", "1440p")
        self.cmb_res.addItem("4K 超清 (更吃性能)", "4k")
        form.addWidget(self.cmb_res, 0, 1)

        form.addWidget(QLabel("帧率"), 0, 2)
        self.spin_fps = QSpinBox()
        # 12–60: common capture rates (24 film / 30 / 60 smooth); higher = larger files
        self.spin_fps.setRange(12, 60)
        self.spin_fps.setValue(20)
        self.spin_fps.setSingleStep(1)
        self.spin_fps.setSuffix(" fps")
        self.spin_fps.setToolTip(
            "帧率 12–60（推荐 20：音画更易同步；24/30 更流畅但电脑吃力时可能画面偏快）"
        )
        form.addWidget(self.spin_fps, 0, 3)

        form.addWidget(QLabel("录制目标"), 1, 0)
        self.cmb_target = QComboBox()
        self.cmb_target.setMinimumWidth(200)
        form.addWidget(self.cmb_target, 1, 1, 1, 2)
        btn_ref_t = QPushButton("刷新", objectName="soft")
        btn_ref_t.clicked.connect(self._refresh_targets)
        form.addWidget(btn_ref_t, 1, 3)

        form.addWidget(QLabel("筛选"), 2, 0)
        self.cmb_filter = QComboBox()
        self.cmb_filter.addItem("全部（屏幕+窗口）", "all")
        self.cmb_filter.addItem("仅显示器", "screen")
        self.cmb_filter.addItem("仅窗口/软件", "window")
        self.cmb_filter.addItem("仅浏览器", "browser")
        # persist wiring connects filter → refresh + save (see _wire_settings_persist)
        form.addWidget(self.cmb_filter, 2, 1, 1, 3)

        form.addWidget(QLabel("麦克风"), 3, 0)
        self.cmb_mic = QComboBox()
        self.cmb_mic.setToolTip("选不同输入设备后可用下方「试听」确认能否录入你的声音")
        form.addWidget(self.cmb_mic, 3, 1, 1, 3)

        form.addWidget(QLabel("系统声音"), 4, 0)
        self.cmb_sys = QComboBox()
        form.addWidget(self.cmb_sys, 4, 1, 1, 3)
        lay.addLayout(form)

        # Mic debug: level meter + short buffer playback
        mic_test = QHBoxLayout()
        self.btn_mic_test = QPushButton("试听麦克风", objectName="soft")
        self.btn_mic_test.setCheckable(True)
        self.btn_mic_test.setToolTip("开始后对着麦克风说话，看右侧电平条是否跳动")
        self.btn_mic_test.clicked.connect(self._toggle_mic_test)
        self.btn_mic_play = QPushButton("回放刚才", objectName="soft")
        self.btn_mic_play.setToolTip("播放试听期间录下的最近几秒，确认扬声器能听到")
        self.btn_mic_play.clicked.connect(self._play_mic_buffer)
        self.mic_level = QProgressBar()
        self.mic_level.setRange(0, 100)
        self.mic_level.setValue(0)
        self.mic_level.setTextVisible(True)
        self.mic_level.setFormat("电平 %v%")
        self.mic_level.setMinimumHeight(22)
        self.mic_level.setToolTip("说话时应变绿/变高；一直为 0 = 选错设备或被系统静音")
        mic_test.addWidget(self.btn_mic_test)
        mic_test.addWidget(self.btn_mic_play)
        mic_test.addWidget(self.mic_level, 1)
        lay.addLayout(mic_test)
        self.lbl_mic_hint = QLabel(
            "调试：换麦克风设备 → 点「试听麦克风」说话看电平 → 可选「回放刚才」听是否录入。"
        )
        self.lbl_mic_hint.setObjectName("muted")
        self.lbl_mic_hint.setWordWrap(True)
        lay.addWidget(self.lbl_mic_hint)

        # Cursor / brush widgets (settings defaults; live controls also on floating bar)
        self.chk_cursor = QCheckBox("录制时高亮鼠标指针")
        self.chk_cursor.setChecked(True)
        lay.addWidget(self.chk_cursor)
        self.btn_cursor_color = QPushButton("指针颜色", objectName="soft")
        self.btn_cursor_color.clicked.connect(self._pick_cursor_color)
        self.slider_cursor = QSlider(Qt.Orientation.Horizontal)
        self.slider_cursor.setRange(12, 48)
        self.slider_cursor.setValue(24)
        self.slider_cursor.setFixedWidth(90)
        self.btn_brush = QPushButton("✏️ 画笔标注", objectName="soft")
        self.btn_brush.setCheckable(True)
        self.btn_brush.clicked.connect(self._toggle_brush)
        self.cmb_brush = QComboBox()
        self.cmb_brush.addItems(["红色", "绿色", "黄色", "蓝色", "白色"])
        self.cmb_brush.currentIndexChanged.connect(self._change_brush_color)
        self.slider_brush = QSlider(Qt.Orientation.Horizontal)
        self.slider_brush.setRange(3, 28)
        self.slider_brush.setValue(8)
        self.slider_brush.valueChanged.connect(self._change_brush_size)
        self.btn_clear = QPushButton("清除笔画", objectName="soft")
        self.btn_clear.clicked.connect(self._clear_brush)
        self.lbl_brush_hint = QLabel(
            "开始录制后，底部会弹出悬浮控制条（画笔 / 指针色 / 暂停 / 停止）。"
            if self.embedded
            else "标注：点画笔后在录制区域拖动。"
        )
        self.lbl_brush_hint.setObjectName("muted")
        self.lbl_brush_hint.setWordWrap(True)
        lay.addWidget(self.lbl_brush_hint)
        # Keep advanced brush options available in non-embedded floating panel
        if not self.embedded:
            cur_row = QHBoxLayout()
            cur_row.addWidget(self.btn_cursor_color)
            cur_row.addWidget(QLabel("大小"))
            cur_row.addWidget(self.slider_cursor)
            cur_row.addStretch(1)
            lay.addLayout(cur_row)
            brush_row = QHBoxLayout()
            brush_row.addWidget(self.btn_brush)
            brush_row.addWidget(self.cmb_brush)
            brush_row.addWidget(self.slider_brush)
            brush_row.addWidget(self.btn_clear)
            brush_row.addStretch(1)
            lay.addLayout(brush_row)
        else:
            self.btn_cursor_color.hide()
            self.slider_cursor.hide()
            self.btn_brush.hide()
            self.cmb_brush.hide()
            self.slider_brush.hide()
            self.btn_clear.hide()

        # Save path
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("保存目录"))
        self.txt_save_dir = QLineEdit()
        default_dir = str(Path.home() / "Videos" / "DesktopToolkitRecordings")
        self.txt_save_dir.setText(default_dir)
        self.txt_save_dir.setPlaceholderText("录制结束后的默认保存文件夹")
        btn_dir = QPushButton("浏览…", objectName="soft")
        btn_dir.clicked.connect(self._pick_save_dir)
        path_row.addWidget(self.txt_save_dir, 1)
        path_row.addWidget(btn_dir)
        lay.addLayout(path_row)

        tip = QLabel("预览为实时效果（含指针高亮）。停止后用 ffmpeg 快速封装，不再整片重编码。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lbl_status = QLabel("就绪 · 选好目标后点开始")
        self.lbl_status.setStyleSheet("color: #34d399; font-weight: 700;")
        lay.addWidget(self.lbl_status)

        ctrl = QHBoxLayout()
        self.btn_rec = QPushButton("● 开始录制")
        self.btn_rec.clicked.connect(self._start)
        self.btn_pause = QPushButton("⏸ 暂停", objectName="soft")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._pause_resume)
        self.btn_stop = QPushButton("■ 停止并保存", objectName="danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        ctrl.addWidget(self.btn_rec)
        if self.embedded:
            # pause/stop live on floating control bar only
            self.btn_pause.hide()
            self.btn_stop.hide()
            tip2 = QLabel("点开始后：底部悬浮条控制 画笔 / 指针色 / 暂停 / 停止")
            tip2.setObjectName("muted")
            lay.addWidget(tip2)
        else:
            ctrl.addWidget(self.btn_pause)
            ctrl.addWidget(self.btn_stop)
        lay.addLayout(ctrl)

        root.addWidget(box)

    def _load_settings(self) -> None:
        """Restore remembered recorder UI options (except live device/target lists)."""
        cfg = self._cfg()
        if cfg.get("save_dir"):
            self.txt_save_dir.setText(str(cfg["save_dir"]))

        res = str(cfg.get("resolution") or "1080p")
        idx = self.cmb_res.findData(res)
        if idx >= 0:
            self.cmb_res.setCurrentIndex(idx)

        try:
            self.spin_fps.setValue(int(cfg.get("fps") or 20))
        except Exception:
            pass

        filt = str(cfg.get("filter") or "all")
        fidx = self.cmb_filter.findData(filt)
        if fidx >= 0:
            self.cmb_filter.blockSignals(True)
            self.cmb_filter.setCurrentIndex(fidx)
            self.cmb_filter.blockSignals(False)

        if "highlight_cursor" in cfg:
            self.chk_cursor.setChecked(bool(cfg.get("highlight_cursor")))
        if cfg.get("cursor_color"):
            self._cursor_color = QColor(str(cfg["cursor_color"]))
        try:
            self.slider_cursor.setValue(int(cfg.get("cursor_radius") or 24))
        except Exception:
            pass

        brush = str(cfg.get("brush_color") or "")
        if brush:
            bidx = self.cmb_brush.findText(brush)
            if bidx >= 0:
                self.cmb_brush.setCurrentIndex(bidx)
            if getattr(self, "control_bar", None):
                cidx = self.control_bar.cmb_brush.findText(brush)
                if cidx >= 0:
                    self.control_bar.cmb_brush.setCurrentIndex(cidx)
        try:
            self.slider_brush.setValue(int(cfg.get("brush_size") or 8))
        except Exception:
            pass

        self._update_cursor_btn()

    def _restore_selection(self) -> None:
        """Match previously saved target / mic / system devices after list refresh."""
        cfg = self._cfg()

        # Target: prefer kind+title, then title alone
        want_title = str(cfg.get("target_title") or "")
        want_kind = str(cfg.get("target_kind") or "")
        want_hwnd = int(cfg.get("target_hwnd") or 0)
        if want_title or want_hwnd:
            best = -1
            for i in range(self.cmb_target.count()):
                data = self.cmb_target.itemData(i)
                if not isinstance(data, dict):
                    continue
                title = str(data.get("title") or "")
                kind = str(data.get("kind") or "")
                hwnd = int(data.get("hwnd") or 0)
                if want_hwnd and hwnd and hwnd == want_hwnd:
                    best = i
                    break
                if want_title and title == want_title and (not want_kind or kind == want_kind):
                    best = i
                    break
                if best < 0 and want_title and title == want_title:
                    best = i
            if best >= 0:
                self.cmb_target.setCurrentIndex(best)

        def _pick_device(combo: QComboBox, name_key: str, idx_key: str) -> None:
            name = str(cfg.get(name_key) or "")
            raw_idx = cfg.get(idx_key, None)
            if name:
                for i in range(combo.count()):
                    if combo.itemText(i) == name:
                        combo.setCurrentIndex(i)
                        return
            if raw_idx is None:
                return
            # None means "不录制"
            if raw_idx == "" or raw_idx is False:
                combo.setCurrentIndex(0)
                return
            try:
                want = int(raw_idx)
            except Exception:
                return
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data is not None and int(data) == want:
                    combo.setCurrentIndex(i)
                    return

        _pick_device(self.cmb_mic, "mic_name", "mic_index")
        _pick_device(self.cmb_sys, "sys_name", "sys_index")

    def _wire_settings_persist(self) -> None:
        """Auto-save when the user changes remembered options."""
        self.cmb_res.currentIndexChanged.connect(self._on_settings_changed)
        self.spin_fps.valueChanged.connect(self._on_settings_changed)
        self.cmb_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.cmb_target.currentIndexChanged.connect(self._on_settings_changed)
        self.cmb_mic.currentIndexChanged.connect(self._on_settings_changed)
        self.cmb_sys.currentIndexChanged.connect(self._on_settings_changed)
        self.chk_cursor.stateChanged.connect(self._on_settings_changed)
        self.slider_cursor.valueChanged.connect(self._on_settings_changed)
        self.cmb_brush.currentIndexChanged.connect(self._on_settings_changed)
        self.slider_brush.valueChanged.connect(self._on_settings_changed)
        self.txt_save_dir.editingFinished.connect(self._on_settings_changed)

    def _on_filter_changed(self) -> None:
        self._refresh_targets()
        self._on_settings_changed()

    def _on_settings_changed(self, *_args) -> None:
        if getattr(self, "_loading_settings", False):
            return
        self._save_settings()

    def _save_settings(self) -> None:
        if getattr(self, "_loading_settings", False):
            return
        cfg = self._cfg()
        cfg["save_dir"] = self.txt_save_dir.text().strip()
        cfg["resolution"] = self.cmb_res.currentData() or "1080p"
        cfg["fps"] = int(self.spin_fps.value())
        cfg["filter"] = self.cmb_filter.currentData() or "all"
        cfg["highlight_cursor"] = bool(self.chk_cursor.isChecked())
        cfg["cursor_color"] = self._cursor_color.name()
        cfg["cursor_radius"] = int(self.slider_cursor.value())
        cfg["brush_color"] = self.cmb_brush.currentText() or "红色"
        cfg["brush_size"] = int(self.slider_brush.value())

        target = self._current_target()
        if isinstance(target, dict):
            cfg["target_title"] = str(target.get("title") or "")
            cfg["target_kind"] = str(target.get("kind") or "")
            try:
                cfg["target_hwnd"] = int(target.get("hwnd") or 0)
            except Exception:
                cfg["target_hwnd"] = 0
        else:
            cfg["target_title"] = ""
            cfg["target_kind"] = ""
            cfg["target_hwnd"] = 0

        mic = self.cmb_mic.currentData()
        cfg["mic_name"] = self.cmb_mic.currentText() if mic is not None else ""
        cfg["mic_index"] = None if mic is None else int(mic)
        sysa = self.cmb_sys.currentData()
        cfg["sys_name"] = self.cmb_sys.currentText() if sysa is not None else ""
        cfg["sys_index"] = None if sysa is None else int(sysa)

        try:
            if self.callbacks and hasattr(self.callbacks, "save_state"):
                self.callbacks.save_state()
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        self.lbl_status.setText(text)

    def _pick_save_dir(self) -> None:
        start = self.txt_save_dir.text().strip() or str(Path.home() / "Videos")
        path = QFileDialog.getExistingDirectory(self, "选择录制保存目录", start)
        if path:
            self.txt_save_dir.setText(path)
            self._save_settings()

    def _pick_cursor_color(self) -> None:
        c = QColorDialog.getColor(self._cursor_color, self, "鼠标高亮颜色")
        if c.isValid():
            self._cursor_color = c
            self._update_cursor_btn()
            self._save_settings()

    def _update_cursor_btn(self) -> None:
        self.btn_cursor_color.setStyleSheet(
            f"QPushButton#soft {{ background: {self._cursor_color.name()}; color: #111; font-weight: 700; }}"
        )

    def _refresh_targets(self) -> None:
        filt = self.cmb_filter.currentData() or "all"
        prev = self._current_target()
        self.cmb_target.blockSignals(True)
        self.cmb_target.clear()
        items: list[dict] = []
        try:
            if filt in ("all", "screen"):
                items.extend(screen_recorder.get_monitors())
            if filt == "all":
                items.extend(screen_recorder.get_window_list(browsers_only=False))
            elif filt == "window":
                items.extend(screen_recorder.get_window_list(browsers_only=False))
            elif filt == "browser":
                items.extend(screen_recorder.get_window_list(browsers_only=True))
        except Exception as e:
            self._set_status(f"枚举窗口失败: {e}")
            items = screen_recorder.get_monitors()
        for it in items:
            self.cmb_target.addItem(it.get("title") or "?", it)
        # Keep prior selection when refreshing the same filter
        if isinstance(prev, dict):
            want_title = str(prev.get("title") or "")
            want_hwnd = int(prev.get("hwnd") or 0)
            for i in range(self.cmb_target.count()):
                data = self.cmb_target.itemData(i)
                if not isinstance(data, dict):
                    continue
                if want_hwnd and int(data.get("hwnd") or 0) == want_hwnd:
                    self.cmb_target.setCurrentIndex(i)
                    break
                if want_title and str(data.get("title") or "") == want_title:
                    self.cmb_target.setCurrentIndex(i)
                    break
        self.cmb_target.blockSignals(False)

    def _refresh_audio(self) -> None:
        self.cmb_mic.blockSignals(True)
        self.cmb_sys.blockSignals(True)
        self.cmb_mic.clear()
        self.cmb_sys.clear()
        self.cmb_mic.addItem("不录制", None)
        self.cmb_sys.addItem("不录制", None)
        try:
            mics, systems = screen_recorder.get_audio_devices()
            for m in mics:
                self.cmb_mic.addItem(m["name"], m["index"])
            for s in systems:
                self.cmb_sys.addItem(s["name"], s["index"])
            # Defaults only when nothing was remembered yet
            cfg = self._cfg()
            if "mic_index" not in cfg and "mic_name" not in cfg and self.cmb_mic.count() > 1:
                self.cmb_mic.setCurrentIndex(1)
            if "sys_index" not in cfg and "sys_name" not in cfg and self.cmb_sys.count() > 1:
                self.cmb_sys.setCurrentIndex(1)
        except Exception:
            pass
        self.cmb_mic.blockSignals(False)
        self.cmb_sys.blockSignals(False)

    def _current_target(self) -> dict | None:
        data = self.cmb_target.currentData()
        return data if isinstance(data, dict) else None

    def _idle_preview(self) -> None:
        """Show live preview even when not recording."""
        if self.recorder and self.recorder.is_recording:
            return
        # Skip work when page/window not visible (embedded hub especially)
        try:
            if not self.isVisible() or self.preview_label.width() < 8:
                return
        except Exception:
            pass
        target = self._current_target()
        region = screen_recorder.resolve_region(target)
        frame = screen_recorder.capture_bgr(region)
        if frame is None:
            return
        if self.chk_cursor.isChecked():
            c = self._cursor_color
            screen_recorder.draw_cursor_highlight(
                frame,
                region,
                color_bgr=(c.blue(), c.green(), c.red()),
                radius=int(self.slider_cursor.value()),
            )
        # Downscale early to cut UI cost
        small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
        self._show_bgr(small)

    def _on_preview_frame(self, frame) -> None:
        if isinstance(frame, np.ndarray):
            self._show_bgr(frame)

    def _show_bgr(self, frame_bgr: np.ndarray) -> None:
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
            pix = QPixmap.fromImage(qimg).scaled(
                max(8, self.preview_label.width() - 8),
                max(8, self.preview_label.height() - 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            self.preview_label.setPixmap(pix)
        except Exception:
            pass

    def _on_overlay_exit_draw(self) -> None:
        """Called from overlay close button / right-click."""
        self.btn_brush.blockSignals(True)
        self.btn_brush.setChecked(False)
        self.btn_brush.blockSignals(False)
        if self.overlay:
            self.overlay.set_draw_mode(False)
        # keep floating control bar in sync
        if getattr(self, "control_bar", None):
            self.control_bar.btn_brush.blockSignals(True)
            self.control_bar.btn_brush.setChecked(False)
            self.control_bar.btn_brush.setText("画笔开")
            self.control_bar.btn_brush.blockSignals(False)
        self.lbl_brush_hint.setText("标注用法：点「画笔标注」→ 在录制区域拖动画线（无青色阴影）")
        self.lbl_brush_hint.setStyleSheet("")
        self._set_status("画笔已关闭")
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _toggle_brush(self) -> None:
        if self.btn_brush.isChecked():
            self._ensure_overlay()
            if self.overlay:
                try:
                    self.overlay.exit_requested.disconnect(self._on_overlay_exit_draw)
                except Exception:
                    pass
                self.overlay.exit_requested.connect(self._on_overlay_exit_draw)
                self.overlay.brush_size = int(self.slider_brush.value())
                self._change_brush_color()
                # Panel can stay open; overlay is topmost over the capture region only
                self.overlay.set_draw_mode(True)
                self.overlay.raise_()
            self.lbl_brush_hint.setText(
                "✅ 画笔已开：在录制区域拖动画线（无青色遮罩，不会录进视频）。"
                "右上角「关闭画笔」或右键可关。"
            )
            self.lbl_brush_hint.setStyleSheet("color: #fbbf24; font-weight: 700;")
            self._set_status("✏️ 画笔已开 · 区域拖动画线 · 右上角可关闭")
        else:
            if self.overlay:
                self.overlay.set_draw_mode(False)
            self.lbl_brush_hint.setText("标注用法：点「画笔标注」→ 在录制区域拖动画线（无青色阴影）")
            self.lbl_brush_hint.setStyleSheet("")
            self._set_status("画笔关闭：鼠标可正常点击下方窗口")

    def _change_brush_color(self) -> None:
        if not self.overlay:
            return
        m = {
            "红色": QColor(239, 68, 68),
            "绿色": QColor(16, 185, 129),
            "黄色": QColor(245, 158, 11),
            "蓝色": QColor(56, 189, 248),
            "白色": QColor(248, 250, 252),
        }
        self.overlay.brush_color = m.get(self.cmb_brush.currentText(), QColor(239, 68, 68))
        if self.overlay.is_draw_mode:
            self.overlay._make_pen_cursor()

    def _change_brush_size(self, v: int) -> None:
        if self.overlay:
            self.overlay.brush_size = int(v)
            if self.overlay.is_draw_mode:
                self.overlay._make_pen_cursor()
                self.overlay.update()

    def _clear_brush(self) -> None:
        if self.overlay:
            self.overlay.clear_canvas()
            self._set_status("已清除标注笔画")

    def _ensure_overlay(self) -> None:
        if self.overlay is None:
            self.overlay = RecordingDrawOverlay()
            try:
                self.overlay.exit_requested.connect(self._on_overlay_exit_draw)
            except Exception:
                pass
        region = screen_recorder.resolve_region(self._current_target())
        if not region:
            try:
                mons = screen_recorder.get_monitors()
                # monitors[0] is virtual all; prefer mon1 primary
                region = mons[1] if len(mons) > 1 else (mons[0] if mons else None)
            except Exception:
                region = None
        if not region:
            # Absolute fallback: primary / virtual screen metrics
            try:
                if HAS_WIN32:
                    x = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
                    y = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
                    w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
                    h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
                    region = {"left": x, "top": y, "width": max(2, w), "height": max(2, h)}
                else:
                    scr = QApplication.primaryScreen()
                    g = scr.geometry() if scr else None
                    if g is not None:
                        region = {
                            "left": int(g.x()),
                            "top": int(g.y()),
                            "width": max(2, int(g.width())),
                            "height": max(2, int(g.height())),
                        }
                    else:
                        region = {"left": 0, "top": 0, "width": 1920, "height": 1080}
            except Exception:
                region = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        self.overlay.set_region(region)
        self.overlay._ensure_canvas()
        if not self.overlay.isVisible():
            self.overlay.show()
        self.overlay.raise_()

    def _on_mic_level(self, level: float) -> None:
        try:
            v = int(max(0, min(100, level)))
            self.mic_level.setValue(v)
            # Color hint: grey idle, green when speaking
            if v < 3:
                col = "#334155"
            elif v < 25:
                col = "#0ea5e9"
            else:
                col = "#22c55e"
            self.mic_level.setStyleSheet(
                f"QProgressBar {{ border:1px solid #334155; border-radius:6px; text-align:center; color:#e2e8f0; }}"
                f"QProgressBar::chunk {{ background:{col}; border-radius:5px; }}"
            )
        except Exception:
            pass

    def _stop_mic_test(self) -> None:
        if self._mic_tester is not None:
            try:
                self._mic_tester.stop()
            except Exception:
                pass
            self._mic_tester = None
        if hasattr(self, "btn_mic_test"):
            self.btn_mic_test.setChecked(False)
            self.btn_mic_test.setText("试听麦克风")
        if hasattr(self, "mic_level"):
            self.mic_level.setValue(0)

    def _toggle_mic_test(self) -> None:
        if self.recorder and self.recorder.is_recording:
            self.btn_mic_test.setChecked(False)
            self._set_status("录制中无法试听麦克风，请先停止录制")
            return
        if self.btn_mic_test.isChecked():
            mic = self.cmb_mic.currentData()
            if mic is None:
                self.btn_mic_test.setChecked(False)
                self._set_status("请先在「麦克风」里选择一个输入设备（不要选「不录制」）")
                return
            self._stop_mic_test()
            self.btn_mic_test.setChecked(True)
            self.btn_mic_test.setText("停止试听")
            self._mic_tester = screen_recorder.MicTester(
                on_level=lambda v: self._bridge.mic_level.emit(float(v)),
                on_status=lambda m: self._bridge.status.emit(str(m)),
            )
            try:
                self._mic_tester.start(int(mic))
            except Exception as e:
                self._stop_mic_test()
                self._set_status(f"试听失败：{e}")
        else:
            self._stop_mic_test()
            self._set_status("已停止麦克风试听（可用「回放刚才」验证）")

    def _play_mic_buffer(self) -> None:
        if self._mic_tester is None:
            # Create a one-shot tester just for message
            self._set_status("请先点「试听麦克风」说几句话，再点「回放刚才」")
            return
        self._mic_tester.play_buffer_async()

    def _start(self) -> None:
        if self.recorder and self.recorder.is_recording:
            return
        self._stop_mic_test()  # free the mic device before real recording
        self._save_settings()
        target = self._current_target()
        if not target:
            self._set_status("请选择录制目标")
            return
        res = self.cmb_res.currentData() or "1080p"
        fps = int(self.spin_fps.value())
        mic = self.cmb_mic.currentData()
        sysa = self.cmb_sys.currentData()
        if mic is None and sysa is None:
            self._set_status("未选择麦克风/系统声音 → 将保存为无声视频。请在上方下拉框选择设备。")
            # Still allow recording (some users want silent capture), but make it obvious.
        c = self._cursor_color

        self._ensure_overlay()
        if self.overlay:
            # Keep existing strokes if brush already used; only clear when starting fresh without brush
            if not self.btn_brush.isChecked():
                self.overlay.clear_canvas()
            reg = screen_recorder.resolve_region(target)
            if reg:
                self.overlay.set_region(reg)
            if self.btn_brush.isChecked():
                self.overlay.set_draw_mode(True)

        def overlay_provider():
            if self.overlay is None:
                return None
            return self.overlay.overlay_rgba

        def preview_cb(frame):
            self._bridge.frame.emit(frame)

        self.recorder = screen_recorder.ScreenRecorder(
            target=target,
            mic_idx=mic,
            sys_idx=sysa,
            fps=fps,
            resolution=res,
            highlight_cursor=self.chk_cursor.isChecked(),
            cursor_color_bgr=(c.blue(), c.green(), c.red()),
            cursor_radius=int(self.slider_cursor.value()),
            preview_cb=preview_cb,
            overlay_provider=overlay_provider,
        )
        try:
            self.recorder.start()
        except Exception as e:
            self._set_status(f"启动失败: {e}")
            return

        self.btn_rec.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        for w in (self.cmb_target, self.cmb_res, self.cmb_mic, self.cmb_sys, self.cmb_filter, self.spin_fps):
            w.setEnabled(False)
        self.duration_timer.start(500)
        self._set_status("🔴 录制中 00:00")
        # Quiet UI: hide settings/hub so recording is not cluttered; only control bar stays
        self._enter_silent_recording_ui()
        try:
            self.control_bar.btn_brush.setChecked(self.btn_brush.isChecked())
            self.control_bar.set_recording_ui(True, paused=False)
        except Exception:
            pass

    def _pause_resume(self) -> None:
        if not self.recorder:
            return
        if self.recorder.is_paused:
            self.recorder.resume()
            self.btn_pause.setText("⏸ 暂停")
            self._set_status(f"🔴 录制中 {self._fmt(self.recorder.duration_seconds)}")
            try:
                self.control_bar.set_recording_ui(True, paused=False)
            except Exception:
                pass
        else:
            self.recorder.pause()
            self.btn_pause.setText("▶ 继续")
            self._set_status(f"⏸ 已暂停 {self._fmt(self.recorder.duration_seconds)}")
            try:
                self.control_bar.set_recording_ui(True, paused=True)
            except Exception:
                pass

    def _stop(self) -> None:
        """Stop capture immediately, then save — no lingering 'recording/mux' step on UI."""
        if not self.recorder or not self.recorder.is_recording or self._busy_stop:
            return
        self._busy_stop = True
        self.duration_timer.stop()
        rec = self.recorder

        # 1) Halt capture right away (leave silent UI / overlay)
        try:
            if self.overlay:
                self.overlay.set_draw_mode(False)
                self.overlay.hide()
        except Exception:
            pass
        try:
            self.control_bar.set_recording_ui(False)
        except Exception:
            pass
        self.btn_rec.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self._set_status("已停止 · 选择保存位置…")
        QApplication.processEvents()

        try:
            rec.halt_capture()
        except Exception as e:
            self._bridge.finished.emit(f"停止失败：{e}")
            return

        # 2) Ask where to save (recording already finished — no more capture)
        save_dir = self.txt_save_dir.text().strip() or str(Path.home() / "Videos")
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        default_name = Path(save_dir) / f"录屏_{self._timestamp()}.mp4"
        path, _ = QFileDialog.getSaveFileName(
            self, "保存录屏", str(default_name), "MP4 视频 (*.mp4)"
        )

        def worker():
            try:
                if path:
                    msg = rec.export(path)
                else:
                    msg = rec.discard()
            except Exception as e:
                msg = f"保存失败：{e}"
            self._bridge.finished.emit(msg)

        # 3) Mux off the UI thread without showing a long "录制保存中" phase
        threading.Thread(target=worker, daemon=True).start()

    def _enter_silent_recording_ui(self) -> None:
        """Minimize settings/main window noise while recording (keep floating control bar)."""
        self._ui_hidden_for_rec = True
        try:
            self.preview_timer.stop()
        except Exception:
            pass
        # Floating settings board
        if not self.embedded:
            try:
                self.hide()
            except Exception:
                pass
        # Main hub if embedded or parent window visible
        try:
            host = getattr(self.callbacks, "host", None) if self.callbacks else None
            # callbacks is SimpleNamespace / host methods — main window via QApplication top levels
            from PyQt6.QtWidgets import QApplication

            for w in QApplication.topLevelWidgets():
                name = type(w).__name__
                # Hide hub main window; keep control bar + overlay + tray
                if name in ("MainWindow",) and w.isVisible():
                    w.setProperty("_dt_hidden_for_rec", True)
                    w.hide()
        except Exception:
            pass
        # Also hide non-embedded board if parent is hub page — parent is MainWindow content
        try:
            p = self.window()
            if p is not None and p is not self and type(p).__name__ == "MainWindow" and p.isVisible():
                p.setProperty("_dt_hidden_for_rec", True)
                p.hide()
        except Exception:
            pass

    def _leave_silent_recording_ui(self) -> None:
        if not getattr(self, "_ui_hidden_for_rec", False):
            return
        self._ui_hidden_for_rec = False
        try:
            import sys as _sys

            self.preview_timer.start(750 if _sys.platform == "darwin" else 500)
        except Exception:
            pass
        try:
            from PyQt6.QtWidgets import QApplication

            for w in QApplication.topLevelWidgets():
                if w.property("_dt_hidden_for_rec"):
                    w.setProperty("_dt_hidden_for_rec", False)
                    w.show()
                    w.raise_()
        except Exception:
            pass
        if not self.embedded:
            try:
                self.show()
                self.raise_()
            except Exception:
                pass

    def _on_save_finished(self, msg: str) -> None:
        self._busy_stop = False
        self.recorder = None
        if self.overlay:
            self.overlay.hide()
            self.overlay.set_draw_mode(False)
        self.btn_brush.setChecked(False)
        self.btn_rec.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("⏸ 暂停")
        self.btn_stop.setEnabled(False)
        for w in (self.cmb_target, self.cmb_res, self.cmb_mic, self.cmb_sys, self.cmb_filter, self.spin_fps):
            w.setEnabled(True)
        self._set_status(msg)
        self._refresh_targets()
        try:
            self.control_bar.set_recording_ui(False)
            self.control_bar.btn_brush.setChecked(False)
        except Exception:
            pass
        self._leave_silent_recording_ui()

    def _tick(self) -> None:
        if self.recorder and self.recorder.is_recording:
            self.recorder._update_duration()
            t = self._fmt(self.recorder.duration_seconds)
            if self.recorder.is_paused:
                self._set_status(f"⏸ 已暂停 {t}")
            else:
                self._set_status(f"🔴 录制中 {t}")

    @staticmethod
    def _fmt(sec: float) -> str:
        s = int(sec)
        return f"{s // 60:02d}:{s % 60:02d}"

    @staticmethod
    def _timestamp() -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.dragging = False

    def hideEvent(self, event) -> None:
        self._save_settings()
        # Don't kill active recording on accidental hide — only hide overlay if idle
        if not (self.recorder and self.recorder.is_recording):
            self._stop_mic_test()
            if self.overlay:
                self.overlay.hide()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        if self.recorder and self.recorder.is_recording:
            self._set_status("请先停止录制再关闭")
            event.ignore()
            return
        self._stop_mic_test()
        if self.overlay:
            self.overlay.hide()
            self.overlay.deleteLater()
            self.overlay = None
        super().closeEvent(event)
