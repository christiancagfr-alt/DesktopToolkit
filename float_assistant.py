"""Draggable floating robot assistant with hover quick-launch menu."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer, QSize
from PyQt6.QtGui import QCursor, QGuiApplication, QIcon, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from skin import bundle_root
from win_topmost import force_topmost


def robot_icon_path() -> Path:
    for name in ("robot_assistant.png", "robot_assistant.jpg", "logo.png"):
        p = bundle_root() / "assets" / name
        if p.is_file():
            return p
        p2 = bundle_root() / name
        if p2.is_file():
            return p2
    return bundle_root() / "logo.png"


def default_float_assistant_enabled() -> bool:
    # macOS users complained about logo stuck on screen — default off there
    return sys.platform != "darwin"


class FloatingAssistant(QWidget):
    """Desktop robot logo (draggable; remembers position). Hover shows tool shortcuts."""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self._drag = False
        self._drag_moved = False
        self._drag_offset = QPoint()
        self._menu_visible = False
        self.setWindowTitle("Toolkit Assistant")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(78, 78)
        self.setToolTip("拖动移动 · 单击打开/关闭菜单 · 双击打开主界面")

        # Pure transparent — no circle, no white plate
        self.icon_lbl = QLabel(self)
        self.icon_lbl.setFixedSize(78, 78)
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setStyleSheet(
            "QLabel { background: transparent; border: none; }"
        )
        # Let press/move/release hit the parent so drag works (label otherwise eats events)
        self.icon_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        path = robot_icon_path()
        if path.is_file():
            pix = QPixmap(str(path))
            # Prefer smooth scaled transparent PNG
            pix = pix.scaled(
                74,
                74,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.icon_lbl.setPixmap(pix)
            self.setWindowIcon(QIcon(str(path)))
        else:
            self.icon_lbl.setText("🤖")
            self.icon_lbl.setStyleSheet(
                "QLabel { background: transparent; border: none; font-size: 36px; }"
            )

        # Hover menu (separate top-level so it can sit above logo)
        self.menu = QFrame(None)
        self.menu.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.menu.setStyleSheet(
            """
            QFrame#assistMenu {
                background: rgba(15,23,42,0.96);
                border: 1px solid #6366f1;
                border-radius: 14px;
            }
            QLabel#assistTitle {
                color: #a5b4fc; font-weight: 800; font-size: 12px; padding: 2px 4px;
            }
            QPushButton {
                background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
                border-radius: 8px; padding: 8px 12px; text-align: left;
                font-weight: 700; font-size: 12px;
            }
            QPushButton:hover {
                background: #6366f1; color: white; border-color: #818cf8;
            }
            """
        )
        self.menu.setObjectName("assistMenu")
        shell = QFrame(self.menu)
        shell.setObjectName("assistMenu")
        outer = QVBoxLayout(self.menu)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        ml = QVBoxLayout(shell)
        ml.setContentsMargins(10, 10, 10, 10)
        ml.setSpacing(4)
        title = QLabel("快捷工具")
        title.setObjectName("assistTitle")
        ml.addWidget(title)

        items = [
            ("待办事项", self._act_todos),
            ("便签", self._act_notes),
            ("笔记本", self._act_notebook),
            ("文件整理", self._act_organize),
            ("远程控制", self._act_remote),
            ("番茄钟", self._act_pomo),
            ("闹钟", self._act_alarm),
            ("天气播报", self._act_weather),
            ("区域截图", self._act_shot),
            ("录屏", self._act_record),
            ("音乐播放器", self._act_music),
            ("清理电脑（立即执行）", self._act_clean),
            ("打开主界面", self._act_hub),
        ]
        for text, slot in items:
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            ml.addWidget(b)
        self.menu.adjustSize()
        self.menu.hide()

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._maybe_hide_menu)

        # Other always-on-top windows can steal z-order; reassert gently.
        # Never while dragging — SetWindowPos would interrupt mouse move.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        self._topmost_timer.start(2500)

        self._restore_or_place()
        self.show()
        QTimer.singleShot(0, self._reassert_topmost)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._drag:
            QTimer.singleShot(0, self._reassert_topmost)

    def _reassert_topmost(self) -> None:
        if self._drag:
            return
        force_topmost(self)
        if self.menu.isVisible():
            force_topmost(self.menu)

    def bring_to_front(self) -> None:
        """Tray / prefs: show logo and force it above other windows."""
        self.show()
        self.raise_()
        self._reassert_topmost()

    def _prefs(self) -> dict:
        try:
            return self.host.store.state.setdefault("prefs", {})
        except Exception:
            return {}

    def _clamp_to_screens(self, x: int, y: int) -> QPoint:
        """Keep logo on a visible screen (multi-monitor / resolution change)."""
        screens = QGuiApplication.screens() or []
        pt = QPoint(x, y)
        for scr in screens:
            g = scr.availableGeometry()
            if g.adjusted(-20, -20, 20, 20).contains(pt):
                nx = max(g.left() + 4, min(x, g.right() - self.width() - 4))
                ny = max(g.top() + 4, min(y, g.bottom() - self.height() - 4))
                return QPoint(nx, ny)
        scr = QGuiApplication.primaryScreen()
        if not scr:
            return QPoint(x, y)
        g = scr.availableGeometry()
        return QPoint(
            max(g.left() + 4, min(x, g.right() - self.width() - 4)),
            max(g.top() + 4, min(y, g.bottom() - self.height() - 4)),
        )

    def _place_bottom_right(self) -> None:
        scr = QGuiApplication.primaryScreen()
        if not scr:
            return
        g = scr.availableGeometry()
        self.move(g.right() - self.width() - 18, g.bottom() - self.height() - 18)

    def _restore_or_place(self) -> None:
        """Use last dragged position if saved; otherwise default corner once."""
        prefs = self._prefs()
        pos = prefs.get("float_assistant_pos")
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            try:
                p = self._clamp_to_screens(int(pos[0]), int(pos[1]))
                self.move(p)
                return
            except Exception:
                pass
        self._place_bottom_right()

    def _save_pos(self) -> None:
        try:
            prefs = self._prefs()
            prefs["float_assistant_pos"] = [int(self.x()), int(self.y())]
            self.host.store.save_state()
        except Exception:
            pass

    def _position_menu(self) -> None:
        self.menu.adjustSize()
        # Prefer above the logo; if near top of screen, open below so it stays visible
        x = self.x() + self.width() - self.menu.width()
        y_above = self.y() - self.menu.height() - 8
        y_below = self.y() + self.height() + 8
        scr = QGuiApplication.primaryScreen()
        # Prefer the screen that contains the logo
        for s in QGuiApplication.screens() or []:
            if s.availableGeometry().contains(self.frameGeometry().center()):
                scr = s
                break
        if scr:
            g = scr.availableGeometry()
            x = max(g.left() + 8, min(x, g.right() - self.menu.width() - 8))
            if y_above >= g.top() + 8:
                y = y_above
            else:
                y = min(y_below, g.bottom() - self.menu.height() - 8)
                y = max(g.top() + 8, y)
        else:
            y = y_above if y_above > 0 else y_below
        self.menu.move(x, y)

    def _show_menu(self) -> None:
        """Open shortcut menu (click-triggered, not hover)."""
        self._hide_timer.stop()
        self._position_menu()
        self.menu.show()
        self.menu.raise_()
        force_topmost(self)
        force_topmost(self.menu)
        self._menu_visible = True
        # Auto-hide only when leaving the menu itself (not when hovering the logo)
        self.menu.enterEvent = lambda e: self._hide_timer.stop()  # type: ignore
        self.menu.leaveEvent = lambda e: self._hide_timer.start(320)  # type: ignore

    def _hide_menu(self) -> None:
        self._hide_timer.stop()
        self.menu.hide()
        self._menu_visible = False

    def _toggle_menu(self) -> None:
        if self.menu.isVisible():
            self._hide_menu()
        else:
            self._show_menu()

    def _maybe_hide_menu(self) -> None:
        pos = QCursor.pos()
        if self.menu.isVisible() and self.menu.frameGeometry().contains(pos):
            return
        # Keep open if cursor returned to the logo (user may click again)
        if self.frameGeometry().contains(pos):
            return
        self._hide_menu()

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._drag_moved = False
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            # Hide menu while dragging so it never blocks move
            self._hide_menu()
            self.grabMouse()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag and e.buttons() & Qt.MouseButton.LeftButton:
            dest = e.globalPosition().toPoint() - self._drag_offset
            if (dest - self.pos()).manhattanLength() > 3:
                self._drag_moved = True
            self.move(dest)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self._drag:
            try:
                self.releaseMouse()
            except Exception:
                pass
            # Snap into visible area and remember
            p = self._clamp_to_screens(self.x(), self.y())
            self.move(p)
            self._save_pos()
            moved = bool(getattr(self, "_drag_moved", False))
            self._drag = False
            self._drag_moved = False
            self._reassert_topmost()
            # Click (no drag) → toggle menu; drag → only move
            if not moved and e.button() == Qt.MouseButton.LeftButton:
                self._toggle_menu()
            e.accept()
            return
        self._drag = False
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:
        self._hide_menu()
        self._act_hub()
        e.accept()

    # --- actions ---
    def _tip(self, msg: str) -> None:
        try:
            self.host.announce(msg)
        except Exception:
            pass

    def _act_todos(self) -> None:
        self._tip("打开待办事项")
        self.host.show_todos()
        self.menu.hide()

    def _act_notes(self) -> None:
        self._tip("打开便签")
        self.host.show_notes()
        self.menu.hide()

    def _act_notebook(self) -> None:
        self._tip("打开笔记本")
        try:
            self.host.show_notebook()
        except Exception:
            pass
        self.menu.hide()

    def _act_organize(self) -> None:
        self._tip("打开文件整理")
        try:
            self.host.show_file_organizer()
        except Exception:
            pass
        self.menu.hide()

    def _act_remote(self) -> None:
        self._tip("打开远程控制")
        try:
            self.host.show_remote_control()
        except Exception:
            pass
        self.menu.hide()

    def _act_pomo(self) -> None:
        self._tip("打开番茄钟")
        self.host.show_pomodoro()
        self.menu.hide()

    def _act_alarm(self) -> None:
        self._tip("打开闹钟")
        self.host.show_alarm_board()
        self.menu.hide()

    def _act_weather(self) -> None:
        self._tip("正在获取天气…")
        try:
            self.host.announce_weather(force=True)
        except Exception:
            pass
        self.menu.hide()

    def _act_shot(self) -> None:
        self._tip("开始区域截图")
        self.host.start_screenshot_region()
        self.menu.hide()

    def _act_record(self) -> None:
        self._tip("打开录屏")
        # Prefer embedded page if hub open; still show floating settings
        try:
            self.host.show_hub()
            if self.host.main_win:
                self.host.main_win.goto("record")
        except Exception:
            self.host.show_recorder_board()
        self.menu.hide()

    def _act_music(self) -> None:
        self._tip("打开音乐播放器")
        try:
            self.host.show_hub()
            if self.host.main_win:
                self.host.main_win.goto("music")
        except Exception:
            self.host.show_music_player()
        self.menu.hide()

    def _act_clean(self) -> None:
        self._tip("开始清理电脑")
        # 直接执行，不进设置页
        self.host.start_deep_clean()
        self.menu.hide()

    def _act_hub(self) -> None:
        # Hide menu first so it doesn't cover / steal focus from the hub
        self.menu.hide()
        try:
            self.host.show_hub()
        except Exception as e:
            self._tip(f"打开主界面失败：{e}")
            return
        # Light tip after show — don't block opening
        QTimer.singleShot(50, lambda: self._tip("已打开主界面"))
