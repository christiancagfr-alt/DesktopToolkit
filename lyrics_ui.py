import sys
import os
import re
import random
import shutil
import json
from pathlib import Path
from PyQt6.QtCore import Qt, QPoint, QTimer, QUrl, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QIcon, QMouseEvent, QAction
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QHBoxLayout, QVBoxLayout,
    QSlider, QLineEdit, QListWidget, QListWidgetItem, QFileDialog, QCheckBox,
    QComboBox, QSpinBox, QAbstractItemView, QFrame, QSizePolicy, QStackedWidget,
    QButtonGroup,
)
from lyrics_engine import LyricsEngine, download_media, scan_page_for_songs
from download_queue import DownloadQueueManager
from ui_theme import dark_glass_qss, drag_bar_qss, clamp_to_screens
from skin import bundle_root

# Try importing win32 modules for desktop click-through
try:
    import win32gui
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

# Native hotkey imports and filter for Windows
from PyQt6.QtCore import QAbstractNativeEventFilter
try:
    from ctypes import wintypes
    import ctypes
    HAS_CTYPES = True
except ImportError:
    HAS_CTYPES = False

# Hotkey modifiers
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_SPACE = 0x20
VK_LEFT = 0x25
VK_RIGHT = 0x26

class _NativeMusicHotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, callback_play, callback_prev, callback_next) -> None:
        super().__init__()
        self.callback_play = callback_play
        self.callback_prev = callback_prev
        self.callback_next = callback_next
        
    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG" or not HAS_CTYPES:
            return False, 0
        try:
            native_message = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
            
        WM_HOTKEY = 0x0312
        if native_message.message == WM_HOTKEY:
            if native_message.wParam == 1001:
                QTimer.singleShot(0, self.callback_play)
                return True, 0
            elif native_message.wParam == 1002:
                QTimer.singleShot(0, self.callback_prev)
                return True, 0
            elif native_message.wParam == 1003:
                QTimer.singleShot(0, self.callback_next)
                return True, 0
        return False, 0


class OutlinedLabel(QLabel):
    """Custom label that draws text with a thick outline for 100% legibility on any wallpaper."""
    
    def __init__(self, text: str = "", font_size: int = 22, bold: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.outline_color = QColor(0, 0, 0, 220)
        self.text_color = QColor(255, 255, 255)
        
        f = QFont("Microsoft YaHei", font_size)
        f.setBold(bold)
        self.setFont(f)
        
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: transparent;")
        
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        font = self.font()
        painter.setFont(font)
        rect = self.rect()
        text = self.text()
        align = self.alignment()
        
        # Draw outline by drawing text offset in 8 directions
        painter.setPen(self.outline_color)
        for dx, dy in [(-1,-1), (-1,1), (1,-1), (1,1), (-2,0), (2,0), (0,-2), (0,2)]:
            painter.drawText(rect.translated(dx, dy), align, text)
            
        # Draw main foreground text
        painter.setPen(self.text_color)
        painter.drawText(rect, align, text)
        painter.end()


class FloatingLyricsWindow(QWidget):
    """Transparent always-on-top window for desktop lyrics with hover media controls."""
    mode_cycled = pyqtSignal(int)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        
        self.drag_position = QPoint()
        self.is_locked = False
        self.play_mode = 0  # 0: List Loop, 1: Single Loop, 2: Shuffle
        
        # Layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(15, 8, 15, 8)
        self.main_layout.setSpacing(4)
        
        self.lbl_curr = OutlinedLabel("🎵 桌面音乐播放器已就绪", font_size=23, bold=True, parent=self)
        self.lbl_next = OutlinedLabel("", font_size=17, bold=False, parent=self)
        self.lbl_next.text_color = QColor(220, 220, 220, 170)
        
        self.main_layout.addWidget(self.lbl_curr)
        self.main_layout.addWidget(self.lbl_next)
        
        # Media controls — larger hit targets; always reachable when unlocked
        self.control_widget = QWidget(self)
        self.control_widget.setObjectName("hudControlPanel")
        self.control_widget.setFixedSize(340, 52)
        self.control_widget.setStyleSheet("""
            QWidget#hudControlPanel {
                background-color: rgba(15, 23, 42, 210);
                border: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 26px;
            }
            QPushButton {
                background-color: rgba(30, 41, 59, 180);
                color: #F8FAFC;
                border: 1px solid rgba(148, 163, 184, 0.35);
                border-radius: 18px;
                min-width: 44px;
                max-width: 52px;
                min-height: 40px;
                max-height: 40px;
                font-size: 18px;
                font-weight: 700;
            }
            QPushButton:hover {
                background-color: rgba(16, 185, 129, 230);
                color: #FFFFFF;
                border-color: #34d399;
            }
            QPushButton:pressed {
                background-color: rgba(6, 95, 70, 230);
            }
            QPushButton#hudClose {
                background-color: rgba(127, 29, 29, 200);
                border-color: rgba(248, 113, 113, 0.5);
                font-size: 16px;
            }
            QPushButton#hudClose:hover {
                background-color: rgba(220, 38, 38, 230);
            }
        """)

        self.control_layout = QHBoxLayout(self.control_widget)
        self.control_layout.setContentsMargins(10, 4, 10, 4)
        self.control_layout.setSpacing(8)
        self.control_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_prev = QPushButton("⏮", self.control_widget)
        self.btn_prev.setToolTip("上一首")
        self.btn_play = QPushButton("▶", self.control_widget)
        self.btn_play.setToolTip("播放 / 暂停")
        self.btn_next = QPushButton("⏭", self.control_widget)
        self.btn_next.setToolTip("下一首")
        self.btn_mode = QPushButton("🔁", self.control_widget)
        self.btn_mode.setToolTip("列表循环")
        self.btn_close_hud = QPushButton("✕", self.control_widget)
        self.btn_close_hud.setObjectName("hudClose")
        self.btn_close_hud.setToolTip("关闭桌面歌词（可在音乐页重新开启）")

        self.control_layout.addWidget(self.btn_prev)
        self.control_layout.addWidget(self.btn_play)
        self.control_layout.addWidget(self.btn_next)
        self.control_layout.addWidget(self.btn_mode)
        self.control_layout.addWidget(self.btn_close_hud)

        self.main_layout.addWidget(self.control_widget)
        self.main_layout.setAlignment(self.control_widget, Qt.AlignmentFlag.AlignHCenter)
        # Show controls by default when unlocked so they are usable without tiny hover target
        self.control_widget.show()

        self.resize(780, 150)
        self._center_on_screen()
        self.close_requested = None  # optional Callable set by dashboard
        
    def _center_on_screen(self) -> None:
        screen = self.screen()
        if screen:
            geom = screen.availableGeometry()
            self.move(
                (geom.width() - self.width()) // 2,
                geom.height() - self.height() - 85
            )
            
    def update_lyrics(self, current: str, next_line: str) -> None:
        self.lbl_curr.setText(current)
        self.lbl_next.setText(next_line)
        self.update()
        
    def update_playback_state(self, playing: bool) -> None:
        self.btn_play.setText("⏸" if playing else "▶")
        
    def update_play_mode(self, mode: int) -> None:
        self.play_mode = mode
        icons = ["🔁", "🔂", "🔀"]
        tips = ["列表循环", "单曲循环", "随机播放"]
        if 0 <= mode < 3:
            self.btn_mode.setText(icons[mode])
            self.btn_mode.setToolTip(tips[mode])
            
    def cycle_play_mode(self) -> None:
        next_mode = (self.play_mode + 1) % 3
        self.update_play_mode(next_mode)
        self.mode_cycled.emit(next_mode)
        
    def set_locked(self, locked: bool) -> None:
        """Click-through lock: lyrics stay visible; mouse passes through the window."""
        self.is_locked = locked
        if locked:
            # Hide controls while click-through (can't click them anyway)
            self.control_widget.hide()
        else:
            self.control_widget.show()
        if not HAS_WIN32:
            return
        hwnd = int(self.winId())
        styles = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if locked:
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                styles | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED,
            )
        else:
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, styles & ~win32con.WS_EX_TRANSPARENT)

    def request_close(self) -> None:
        """Close desktop lyrics (works even if user can't click the HUD)."""
        try:
            self.set_locked(False)
        except Exception:
            pass
        self.hide()
        cb = getattr(self, "close_requested", None)
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def enterEvent(self, event) -> None:
        if not self.is_locked:
            self.control_widget.show()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        # Keep controls visible while unlocked so they stay easy to hit
        if self.is_locked:
            self.control_widget.hide()
        super().leaveEvent(event)
        
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.is_locked and event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self.is_locked and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()


class _DownloadQueueBridge(QObject):
    """Marshal download-queue callbacks from worker threads onto the Qt GUI thread."""
    status_changed = pyqtSignal()
    song_ready = pyqtSignal(object, object)
    log_message = pyqtSignal(str)


class LyricsDashboard(QWidget):
    """Modern glass music player: foldable, resizable, position-aware."""

    COLLAPSED_H = 44
    COLLAPSED_W = 280

    def __init__(
        self,
        engine: LyricsEngine,
        hud: FloatingLyricsWindow,
        parent: QWidget | None = None,
        state: dict | None = None,
        callbacks=None,
        *,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.embedded = embedded
        self.engine = engine
        self.hud = hud
        self.state = state
        self.callbacks = callbacks

        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.setMinimumSize(480, 400)
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setMinimumSize(self.COLLAPSED_W, self.COLLAPSED_H)
        
        self.drag_position = QPoint()
        self.playlist: list[Path] = []
        self.current_song_idx = -1
        self.is_dragging_slider = False
        self.play_mode = 0  # 0: List Loop, 1: Single Loop, 2: Shuffle
        self._collapsed = False
        self._expand_size = (560, 620)
        self._resize_origin: QPoint | None = None
        self._resize_size: tuple[int, int] | None = None
        
        # Writable user data (never write under Program Files / _MEIPASS)
        self.user_data_root = self._user_data_root()
        self.settings_file = self.user_data_root / "lyrics_settings.json"
        default_music_dir = self.user_data_root / "lyrics_music"
        self.music_dir = default_music_dir
        self._saved_device_id: str = ""
        self._saved_auto_switch = True
        self._cfg: dict = {}

        if self.settings_file.exists():
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    self._cfg = json.load(f) or {}
                    saved_dir = self._cfg.get("music_dir")
                    if saved_dir and Path(saved_dir).exists():
                        self.music_dir = Path(saved_dir)
                    self._saved_device_id = str(self._cfg.get("audio_device_id") or "")
                    self._saved_auto_switch = bool(self._cfg.get("audio_auto_switch", True))
                    self._collapsed = bool(self._cfg.get("collapsed", False))
                    self._expand_size = (
                        max(480, int(self._cfg.get("win_w") or 560)),
                        max(520, int(self._cfg.get("win_h") or 620)),
                    )
            except Exception as e:
                print(f"Error loading lyrics settings: {e}")

        try:
            self.music_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.music_dir = self.user_data_root / "lyrics_music"
            self.music_dir.mkdir(parents=True, exist_ok=True)

        # Parallel download queue (pause/resume + daily source scan)
        self._dl_bridge = _DownloadQueueBridge(self)
        self._dl_bridge.status_changed.connect(self._refresh_download_ui)
        self._dl_bridge.song_ready.connect(self._on_song_downloaded)
        self._dl_bridge.log_message.connect(self._on_dl_log)
        self.dl_queue = DownloadQueueManager(self.music_dir, max_workers=3)
        self.dl_queue.status_changed = lambda: self._dl_bridge.status_changed.emit()
        self.dl_queue.song_ready = lambda a, l: self._dl_bridge.song_ready.emit(a, l)
        self.dl_queue.log = lambda m: self._dl_bridge.log_message.emit(m)
        self.dl_queue.start()

        self._init_ui()

        # Restore geometry / position
        if self._cfg.get("win_x") is not None and self._cfg.get("win_y") is not None:
            try:
                self.move(int(self._cfg["win_x"]), int(self._cfg["win_y"]))
            except (TypeError, ValueError):
                pass
        if self._collapsed:
            self._apply_collapsed(True, persist=False)
        else:
            self.resize(*self._expand_size)
        clamp_to_screens(self)

        # Connect engine signals
        self.engine.lyric_changed.connect(self._on_lyric_changed)
        self.engine.position_changed.connect(self._on_position_changed)
        self.engine.duration_changed.connect(self._on_duration_changed)
        self.engine.playback_state_changed.connect(self._on_playback_state_changed)
        self.engine.song_ended.connect(self._on_song_ended)
        self.engine.set_auto_switch(self._saved_auto_switch)

        # Connect HUD buttons
        self.hud.close_requested = self._on_hud_close_requested
        self.hud.btn_close_hud.clicked.connect(self._on_hud_close_requested)
        self.hud.btn_prev.clicked.connect(self.play_prev)
        self.hud.btn_play.clicked.connect(self.toggle_play)
        self.hud.btn_next.clicked.connect(self.play_next)
        self.hud.btn_mode.clicked.connect(self.hud.cycle_play_mode)
        self.hud.mode_cycled.connect(self.cmb_mode.setCurrentIndex)

        # Register global hotkeys on Windows (Ctrl+Alt+Space/Left/Right)
        self.hotkeys_registered = False
        if sys.platform == "win32":
            try:
                user32 = ctypes.windll.user32
                self._hotkey_filter = _NativeMusicHotkeyFilter(self.toggle_play, self.play_prev, self.play_next)
                QApplication.instance().installNativeEventFilter(self._hotkey_filter)

                r_play = user32.RegisterHotKey(None, 1001, MOD_CONTROL | MOD_ALT, VK_SPACE)
                r_prev = user32.RegisterHotKey(None, 1002, MOD_CONTROL | MOD_ALT, VK_LEFT)
                r_next = user32.RegisterHotKey(None, 1003, MOD_CONTROL | MOD_ALT, VK_RIGHT)
                
                self.hotkeys_registered = bool(r_play and r_prev and r_next)
                print(f"Global media hotkeys registered: {self.hotkeys_registered}", flush=True)
            except Exception as e:
                print(f"Failed to register global hotkeys: {e}", flush=True)
                
        self._refresh_playlist()
        self._refresh_download_ui()
        self._refresh_audio_devices(select_saved=True)
        
        # Restore lyric color from state
        if self.state and hasattr(self, "color_presets") and hasattr(self, "cmb_color"):
            cfg = self.state.get("lyrics_player") or {}
            custom_hex = str(cfg.get("lyric_color_hex") or "").strip()
            if custom_hex:
                c = QColor(custom_hex)
                if c.isValid():
                    self._custom_lyric_color = c
                    self.color_presets["自定义"] = c
            saved_color = cfg.get("lyric_color_name")
            if saved_color and saved_color in self.color_presets:
                self.cmb_color.setCurrentText(saved_color)
                self._apply_lyric_color()
            elif custom_hex:
                self.cmb_color.setCurrentText("自定义")
                self._apply_lyric_color()
                
        # Refresh device list when outputs change (BT connect/disconnect)
        try:
            from PyQt6.QtMultimedia import QMediaDevices
            QMediaDevices.audioOutputsChanged.connect(lambda: self._refresh_audio_devices(select_saved=False))
        except Exception:
            pass

    @staticmethod
    def _user_data_root() -> Path:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        root = Path(base) / "DesktopToolkit"
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except OSError:
            fallback = Path.home() / ".DesktopToolkit"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_play()
            event.accept()
        elif key == Qt.Key.Key_Left:
            self.play_prev()
            event.accept()
        elif key == Qt.Key.Key_Right:
            self.play_next()
            event.accept()
        elif key == Qt.Key.Key_Up:
            # Volume up (5% increments)
            self.slider_vol.setValue(min(100, self.slider_vol.value() + 5))
            event.accept()
        elif key == Qt.Key.Key_Down:
            # Volume down (5% decrements)
            self.slider_vol.setValue(max(0, self.slider_vol.value() - 5))
            event.accept()
        else:
            super().keyPressEvent(event)
            
    def closeEvent(self, event) -> None:
        # Window is usually only hidden; only stop the queue when the app truly closes.
        self.unregister_hotkeys()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Called by main on app exit — stop download workers cleanly."""
        try:
            self.unregister_hotkeys()
        except Exception:
            pass
        try:
            self.dl_queue.stop()
        except Exception:
            pass
        
    def unregister_hotkeys(self) -> None:
        if sys.platform == "win32" and getattr(self, "hotkeys_registered", False):
            try:
                user32 = ctypes.windll.user32
                user32.UnregisterHotKey(None, 1001)
                user32.UnregisterHotKey(None, 1002)
                user32.UnregisterHotKey(None, 1003)
                self.hotkeys_registered = False
                print("Global media hotkeys unregistered.", flush=True)
            except Exception as e:
                print(f"Failed to unregister global hotkeys: {e}", flush=True)
        
    def _init_ui(self) -> None:
        self.setStyleSheet(
            dark_glass_qss()
            + """
            QFrame#sideNav {
                background: rgba(15, 23, 42, 0.55);
                border-right: 1px solid rgba(148, 163, 184, 0.14);
                border-top-left-radius: 0;
                border-bottom-left-radius: 0;
            }
            QPushButton#navBtn {
                background: transparent;
                border: none;
                border-radius: 10px;
                color: #94a3b8;
                text-align: left;
                padding: 10px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#navBtn:hover {
                background: rgba(52, 211, 153, 0.10);
                color: #e2e8f0;
            }
            QPushButton#navBtn:checked {
                background: rgba(52, 211, 153, 0.18);
                color: #6ee7b7;
                border: 1px solid rgba(52, 211, 153, 0.35);
            }
            QFrame#playerBar {
                background: rgba(15, 23, 42, 0.92);
                border-top: 1px solid rgba(148, 163, 184, 0.16);
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
            }
            QFrame#pageCard {
                background: transparent;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        frame = QFrame(self)
        frame.setObjectName("MainFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        # --- Title bar (hidden when embedded in main window) ---
        title_bar = QFrame(frame)
        title_bar.setObjectName("dragBar")
        title_bar.setStyleSheet(drag_bar_qss(dark=True))
        title_bar.setFixedHeight(40)
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(12, 0, 6, 0)
        tb.setSpacing(4)
        self.lbl_window_title = QLabel("♪  桌面音乐")
        self.lbl_window_title.setStyleSheet(
            "color: #34d399; font-weight: 700; font-size: 13px; background: transparent;"
        )
        tb.addWidget(self.lbl_window_title, 1)
        self.btn_collapse = QPushButton("▼")
        self.btn_collapse.setObjectName("iconBtn")
        self.btn_collapse.setToolTip("折叠 / 展开")
        self.btn_collapse.clicked.connect(self.toggle_collapse)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("iconBtn")
        btn_close.setToolTip("隐藏")
        btn_close.clicked.connect(self._on_hide)
        tb.addWidget(self.btn_collapse)
        tb.addWidget(btn_close)
        title_bar.mousePressEvent = self._title_press  # type: ignore
        title_bar.mouseMoveEvent = self._title_move  # type: ignore
        title_bar.mouseReleaseEvent = self._title_release  # type: ignore
        title_bar.mouseDoubleClickEvent = self._title_dbl  # type: ignore
        if not self.embedded:
            frame_layout.addWidget(title_bar)
        else:
            title_bar.hide()

        # --- Collapsible body: left nav | right pages | bottom player ---
        self.body_widget = QWidget(frame)
        body = QVBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)

        # Left navigation
        side = QFrame()
        side.setObjectName("sideNav")
        side.setFixedWidth(112)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(8, 12, 8, 12)
        side_l.setSpacing(6)
        nav_tip = QLabel("板块")
        nav_tip.setObjectName("muted")
        side_l.addWidget(nav_tip)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_btns: list[QPushButton] = []
        nav_items = [
            ("playlist", "🎵  歌单"),
            ("download", "⬇  下载"),
            ("source", "⭐  订阅"),
            ("audio", "🔊  音频"),
        ]
        for i, (key, label) in enumerate(nav_items):
            btn = QPushButton(label)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("page_key", key)
            self._nav_group.addButton(btn, i)
            side_l.addWidget(btn)
            self._nav_btns.append(btn)
        side_l.addStretch(1)
        self._nav_group.idClicked.connect(self._on_nav_changed)
        mid.addWidget(side)

        # Right stacked pages
        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self._build_page_playlist())
        self.page_stack.addWidget(self._build_page_download())
        self.page_stack.addWidget(self._build_page_source())
        self.page_stack.addWidget(self._build_page_audio())
        mid.addWidget(self.page_stack, 1)
        body.addLayout(mid, 1)

        # Fixed bottom player bar (always visible while expanded)
        body.addWidget(self._build_player_bar())

        frame_layout.addWidget(self.body_widget, 1)
        root.addWidget(frame)

        # Default page
        self._nav_btns[0].setChecked(True)
        self.page_stack.setCurrentIndex(0)

        # Resize grip
        self._grip = QWidget(self)
        self._grip.setFixedSize(16, 16)
        self._grip.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._grip.setStyleSheet("background: rgba(52, 211, 153, 0.55); border-radius: 3px;")
        self._grip.mousePressEvent = self._grip_press  # type: ignore
        self._grip.mouseMoveEvent = self._grip_move  # type: ignore
        self._grip.mouseReleaseEvent = self._grip_release  # type: ignore

        # Expand default size for split layout
        if self._expand_size[0] < 520:
            self._expand_size = (560, max(560, self._expand_size[1]))

    def _on_nav_changed(self, index: int) -> None:
        if 0 <= index < self.page_stack.count():
            self.page_stack.setCurrentIndex(index)

    def _build_page_playlist(self) -> QWidget:
        page = QWidget()
        page.setObjectName("pageCard")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("本地歌单")
        title.setObjectName("section")
        head.addWidget(title)
        head.addStretch(1)
        btn_import = QPushButton("导入文件")
        btn_import.setObjectName("ghost")
        btn_import.setToolTip("选择一个或多个音频文件导入")
        btn_import.clicked.connect(self._on_import_local_clicked)
        btn_import_folder = QPushButton("导入文件夹")
        btn_import_folder.setObjectName("ghost")
        btn_import_folder.setToolTip("选择文件夹，递归导入其中的音频（含同名 .lrc）")
        btn_import_folder.clicked.connect(self._on_import_folder_clicked)
        btn_open = QPushButton("打开文件夹")
        btn_open.setObjectName("ghost")
        btn_open.clicked.connect(self._on_open_dir_clicked)
        btn_path = QPushButton("更改路径")
        btn_path.setObjectName("ghost")
        btn_path.clicked.connect(self._on_change_dir_clicked)
        head.addWidget(btn_import)
        head.addWidget(btn_import_folder)
        head.addWidget(btn_path)
        head.addWidget(btn_open)
        lay.addLayout(head)
        self.lbl_path = QLabel("")
        self.lbl_path.setObjectName("muted")
        self._update_path_display()
        lay.addWidget(self.lbl_path)
        self.list_songs = QListWidget()
        self.list_songs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list_songs.itemDoubleClicked.connect(self._on_song_double_clicked)
        lay.addWidget(self.list_songs, 1)
        tip = QLabel("双击播放 · 底部控件始终可用，切板块也不影响播放")
        tip.setObjectName("muted")
        lay.addWidget(tip)
        return page

    def _build_page_download(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)
        title = QLabel("下载队列")
        title.setObjectName("section")
        lay.addWidget(title)

        dl_layout = QHBoxLayout()
        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("歌单 / 歌曲链接，多个用 | 分隔…")
        btn_dl = QPushButton("加入队列")
        btn_dl.setObjectName("primary")
        btn_dl.clicked.connect(self._on_download_clicked)
        dl_layout.addWidget(self.txt_url, 1)
        dl_layout.addWidget(btn_dl)
        lay.addLayout(dl_layout)

        dl_ctrl = QHBoxLayout()
        self.btn_pause_all = QPushButton("全部暂停")
        self.btn_pause_all.setObjectName("ghost")
        self.btn_pause_all.clicked.connect(self._on_pause_all)
        self.btn_resume_all = QPushButton("全部继续")
        self.btn_resume_all.setObjectName("ghost")
        self.btn_resume_all.clicked.connect(self._on_resume_all)
        self.btn_del_job = QPushButton("删除选中")
        self.btn_del_job.setObjectName("danger")
        self.btn_del_job.clicked.connect(self._on_delete_selected_jobs)
        self.btn_clear_jobs = QPushButton("清完成")
        self.btn_clear_jobs.setObjectName("ghost")
        self.btn_clear_jobs.clicked.connect(self._on_clear_finished)
        for b in (self.btn_pause_all, self.btn_resume_all, self.btn_del_job, self.btn_clear_jobs):
            dl_ctrl.addWidget(b)
        dl_ctrl.addStretch(1)
        lay.addLayout(dl_ctrl)

        self.lbl_dl_status = QLabel("下载队列空闲")
        self.lbl_dl_status.setObjectName("muted")
        self.lbl_dl_status.setWordWrap(True)
        lay.addWidget(self.lbl_dl_status)

        self.list_jobs = QListWidget()
        self.list_jobs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_jobs.setToolTip("双击：暂停/继续 · 多选后可删除")
        self.list_jobs.itemDoubleClicked.connect(self._on_job_double_clicked)
        lay.addWidget(self.list_jobs, 1)

        scan_row = QHBoxLayout()
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 6)
        self.spin_workers.setValue(int(self.dl_queue.max_workers))
        self.spin_workers.setPrefix("并行 ")
        self.spin_workers.setMinimumHeight(32)
        self.spin_workers.setMinimumWidth(88)
        self.spin_workers.valueChanged.connect(self._on_workers_changed)
        scan_row.addWidget(self.spin_workers)
        scan_row.addStretch(1)
        lay.addLayout(scan_row)
        return page

    def _build_page_source(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)
        title = QLabel("订阅源 · 每日扫描同步歌单")
        title.setObjectName("section")
        lay.addWidget(title)

        src_in = QHBoxLayout()
        self.txt_source_url = QLineEdit()
        self.txt_source_url.setPlaceholderText("粘贴要订阅的歌单 / 页面链接…")
        self.btn_add_source = QPushButton("加入订阅")
        self.btn_add_source.setObjectName("primary")
        self.btn_add_source.clicked.connect(self._on_add_source)
        src_in.addWidget(self.txt_source_url, 1)
        src_in.addWidget(self.btn_add_source)
        lay.addLayout(src_in)

        row = QHBoxLayout()
        self.btn_scan_now = QPushButton("立即扫描全部")
        self.btn_scan_now.setObjectName("ghost")
        self.btn_scan_now.clicked.connect(self._on_scan_now)
        row.addWidget(self.btn_scan_now)
        row.addStretch(1)
        lay.addLayout(row)

        tip = QLabel("订阅后可每日自动扫描新歌入队 · 双击列表项删除订阅")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.list_sources = QListWidget()
        self.list_sources.itemDoubleClicked.connect(self._on_source_double_clicked)
        lay.addWidget(self.list_sources, 1)

        opt = QHBoxLayout()
        self.chk_daily = QCheckBox("每日自动扫描")
        self.chk_daily.setChecked(bool(self.dl_queue.daily_scan))
        self.chk_daily.stateChanged.connect(self._on_daily_scan_toggled)
        self.spin_scan_hour = QSpinBox()
        self.spin_scan_hour.setRange(0, 23)
        self.spin_scan_hour.setValue(int(self.dl_queue.scan_hour))
        self.spin_scan_hour.setPrefix("时 ")
        self.spin_scan_hour.setMinimumHeight(32)
        self.spin_scan_hour.setMinimumWidth(80)
        self.spin_scan_hour.valueChanged.connect(self._on_scan_hour_changed)
        opt.addWidget(self.chk_daily)
        opt.addWidget(self.spin_scan_hour)
        opt.addStretch(1)
        lay.addLayout(opt)
        return page

    def _build_page_audio(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(10)
        title = QLabel("音频与歌词")
        title.setObjectName("section")
        lay.addWidget(title)

        audio_row = QHBoxLayout()
        lbl_dev = QLabel("输出设备")
        lbl_dev.setObjectName("muted")
        self.cmb_device = QComboBox()
        self.cmb_device.setMinimumHeight(34)
        self.cmb_device.setToolTip("蓝牙没电时切换到扬声器")
        self.cmb_device.currentIndexChanged.connect(self._on_device_changed)
        self.chk_auto_audio = QCheckBox("设备断开自动切换")
        self.chk_auto_audio.setChecked(self._saved_auto_switch)
        self.chk_auto_audio.stateChanged.connect(self._on_auto_audio_toggled)
        btn_refresh_dev = QPushButton("刷新")
        btn_refresh_dev.setObjectName("ghost")
        btn_refresh_dev.clicked.connect(lambda: self._refresh_audio_devices(select_saved=False))
        audio_row.addWidget(lbl_dev)
        audio_row.addWidget(self.cmb_device, 1)
        audio_row.addWidget(btn_refresh_dev)
        lay.addLayout(audio_row)
        lay.addWidget(self.chk_auto_audio)

        color_layout = QHBoxLayout()
        lbl_color = QLabel("歌词颜色")
        lbl_color.setObjectName("muted")
        self.cmb_color = QComboBox()
        self.cmb_color.setMinimumHeight(34)
        self.color_presets = {
            "雾白": QColor(248, 250, 252),
            "翡翠绿": QColor(52, 211, 153),
            "荧光绿": QColor(74, 222, 128),
            "琥珀金": QColor(251, 191, 36),
            "天空蓝": QColor(56, 189, 248),
            "紫罗兰": QColor(167, 139, 250),
            "玫瑰粉": QColor(244, 114, 182),
            "自定义": QColor(255, 255, 255),
        }
        for name in self.color_presets.keys():
            self.cmb_color.addItem(name)
        self.cmb_color.currentIndexChanged.connect(self._apply_lyric_color)
        self.btn_custom_color = QPushButton("取色")
        self.btn_custom_color.setObjectName("ghost")
        self.btn_custom_color.setMinimumHeight(34)
        self.btn_custom_color.setMinimumWidth(64)
        self.btn_custom_color.setToolTip("打开取色器，自定义歌词颜色")
        self.btn_custom_color.clicked.connect(self._pick_custom_lyric_color)
        self._custom_lyric_color = QColor(255, 255, 255)
        color_layout.addWidget(lbl_color)
        color_layout.addWidget(self.cmb_color, 1)
        color_layout.addWidget(self.btn_custom_color)
        lay.addLayout(color_layout)

        hud_row = QHBoxLayout()
        self.chk_hud = QCheckBox("显示桌面歌词（软件内开启 / 可随时关闭）")
        self.chk_hud.setChecked(True)
        self.chk_hud.setToolTip("勾选后显示桌面歌词；取消勾选或点歌词上的 ✕ 即可关闭")
        self.chk_hud.stateChanged.connect(self._on_hud_toggled)
        self.chk_lock = QCheckBox("穿透锁定（歌词照常显示，鼠标点穿到下面）")
        self.chk_lock.setChecked(False)
        self.chk_lock.setToolTip("开启后歌词仍可见，但不接收鼠标；关闭请在本页取消勾选「显示桌面歌词」")
        self.chk_lock.stateChanged.connect(self._on_lock_toggled)
        hud_row.addWidget(self.chk_hud)
        hud_row.addWidget(self.chk_lock)
        hud_row.addStretch(1)
        lay.addLayout(hud_row)

        tip = QLabel("播放控制在窗口最下方固定区域，切换左侧板块时始终可见。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch(1)
        return page

    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("playerBar")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(8)

        # Transport
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        self.btn_prev = QPushButton("⏮")
        self.btn_play = QPushButton("▶  播放")
        self.btn_play.setObjectName("primary")
        self.btn_next = QPushButton("⏭")
        self.btn_stop = QPushButton("⏹")
        self.btn_stop.setObjectName("ghost")
        self.btn_prev.clicked.connect(self.play_prev)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next.clicked.connect(self.play_next)
        self.btn_stop.clicked.connect(self.stop_play)
        for b in (self.btn_prev, self.btn_play, self.btn_next, self.btn_stop):
            b.setMinimumHeight(34)
            btn_layout.addWidget(b)
        lay.addLayout(btn_layout)

        # Seek
        progress_layout = QHBoxLayout()
        self.lbl_time_curr = QLabel("00:00")
        self.lbl_time_curr.setObjectName("muted")
        self.slider_seek = QSlider(Qt.Orientation.Horizontal)
        self.slider_seek.setEnabled(False)
        self.slider_seek.sliderPressed.connect(self._on_slider_pressed)
        self.slider_seek.sliderReleased.connect(self._on_slider_released)
        self.slider_seek.sliderMoved.connect(self._on_slider_moved)
        self.lbl_time_dur = QLabel("00:00")
        self.lbl_time_dur.setObjectName("muted")
        progress_layout.addWidget(self.lbl_time_curr)
        progress_layout.addWidget(self.slider_seek, 1)
        progress_layout.addWidget(self.lbl_time_dur)
        lay.addLayout(progress_layout)

        # Volume + mode
        row = QHBoxLayout()
        lbl_vol = QLabel("音量")
        lbl_vol.setObjectName("muted")
        self.slider_vol = QSlider(Qt.Orientation.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(100)
        self.slider_vol.setFixedWidth(110)
        self.slider_vol.valueChanged.connect(self._on_volume_changed)
        lbl_mode = QLabel("模式")
        lbl_mode.setObjectName("muted")
        self.cmb_mode = QComboBox()
        self.cmb_mode.setMinimumHeight(30)
        self.cmb_mode.addItem("🔁 列表")
        self.cmb_mode.addItem("🔂 单曲")
        self.cmb_mode.addItem("🔀 随机")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        row.addWidget(lbl_vol)
        row.addWidget(self.slider_vol)
        row.addSpacing(10)
        row.addWidget(lbl_mode)
        row.addWidget(self.cmb_mode)
        row.addStretch(1)
        lay.addLayout(row)
        return bar


    def toggle_collapse(self) -> None:
        self._apply_collapsed(not self._collapsed, persist=True)

    def _apply_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        self._collapsed = collapsed
        if collapsed:
            if self.body_widget.isVisible() and self.height() > self.COLLAPSED_H + 40:
                self._expand_size = (max(self.COLLAPSED_W, self.width()), max(480, self.height()))
            self.body_widget.hide()
            self._grip.hide()
            self.setMinimumSize(self.COLLAPSED_W, self.COLLAPSED_H)
            self.setMaximumHeight(self.COLLAPSED_H + 4)
            self.resize(max(self.COLLAPSED_W, min(self.width(), 360)), self.COLLAPSED_H)
            self.btn_collapse.setText("▶")
            playing = ""
            try:
                if self.engine.is_playing() and self.current_song_idx >= 0:
                    playing = " · " + self.playlist[self.current_song_idx].stem[:18]
            except Exception:
                pass
            self.lbl_window_title.setText(f"♪  音乐{playing}")
        else:
            self.setMaximumHeight(16777215)
            self.setMinimumSize(480, 520)
            self.body_widget.show()
            self._grip.show()
            self.resize(*self._expand_size)
            self.btn_collapse.setText("▼")
            self.lbl_window_title.setText("♪  桌面音乐")
        if persist:
            self._save_settings()

    def _on_hide(self) -> None:
        self._save_settings()
        # Also close desktop lyrics when hiding the player window
        self._on_hud_close_requested()
        self.hide()

    def _on_hud_close_requested(self) -> None:
        """Close desktop lyrics from HUD ✕ or when player asks."""
        try:
            self.hud.set_locked(False)
        except Exception:
            pass
        try:
            self.hud.hide()
        except Exception:
            pass
        if hasattr(self, "chk_hud"):
            self.chk_hud.blockSignals(True)
            self.chk_hud.setChecked(False)
            self.chk_hud.blockSignals(False)
        if hasattr(self, "chk_lock"):
            self.chk_lock.blockSignals(True)
            self.chk_lock.setChecked(False)
            self.chk_lock.blockSignals(False)
        try:
            self._save_settings()
        except Exception:
            pass

    def _title_press(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def _title_move(self, event) -> None:
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def _title_release(self, event) -> None:
        self._save_settings()
        event.accept()

    def _title_dbl(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_collapse()
            event.accept()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if not self._collapsed and hasattr(self, "_grip"):
            self._grip.move(self.width() - 18, self.height() - 18)

    def _grip_press(self, event) -> None:
        if self._collapsed:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._resize_origin = event.globalPosition().toPoint()
            self._resize_size = (self.width(), self.height())
            event.accept()

    def _grip_move(self, event) -> None:
        if self._collapsed or self._resize_origin is None or self._resize_size is None:
            return
        delta = event.globalPosition().toPoint() - self._resize_origin
        nw = max(480, self._resize_size[0] + delta.x())
        nh = max(520, self._resize_size[1] + delta.y())
        self.resize(nw, nh)
        event.accept()

    def _grip_release(self, event) -> None:
        self._resize_origin = None
        self._resize_size = None
        if not self._collapsed:
            self._expand_size = (self.width(), self.height())
            self._save_settings()
        event.accept()

    def _on_delete_selected_jobs(self) -> None:
        ids = []
        for item in self.list_jobs.selectedItems():
            jid = item.data(Qt.ItemDataRole.UserRole)
            if jid:
                ids.append(str(jid))
        if not ids:
            self.lbl_dl_status.setText("请先选中要删除的队列项（可多选）。")
            return
        msg = self.dl_queue.remove_jobs(ids)
        self.lbl_dl_status.setText(msg)
        self._refresh_download_ui()


    def _update_path_display(self) -> None:
        short_path = f".../{self.music_dir.parent.name}/{self.music_dir.name}"
        if len(short_path) > 30:
            short_path = short_path[-30:]
        self.lbl_path.setText(f"存储路径: {short_path}")
        self.lbl_path.setToolTip(str(self.music_dir))
        
    def _refresh_playlist(self) -> None:
        self.list_songs.clear()
        self.playlist = []
        
        # Scan folder for audio
        extensions = {".mp3", ".wav", ".m4a", ".ogg"}
        if self.music_dir.exists():
            for p in sorted(self.music_dir.iterdir()):
                if p.suffix.lower() in extensions:
                    self.playlist.append(p)
                    # Add to QListWidget
                    item = QListWidgetItem(p.name)
                    self.list_songs.addItem(item)
                    
    def _on_import_local_clicked(self) -> None:
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "选择本地音频文件", "", "音频文件 (*.mp3 *.wav *.m4a *.ogg)"
        )
        if not file_paths:
            return
        self._import_audio_paths([Path(fp) for fp in file_paths])

    def _on_import_folder_clicked(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择要导入的音乐文件夹")
        if not folder:
            return
        root = Path(folder)
        exts = {".mp3", ".wav", ".m4a", ".ogg"}
        paths = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts]
        if not paths:
            try:
                self.lbl_path.setToolTip("该文件夹下没有可导入的音频")
            except Exception:
                pass
            return
        self._import_audio_paths(paths)

    def _import_audio_paths(self, paths: list[Path]) -> None:
        self.music_dir.mkdir(parents=True, exist_ok=True)
        for src_path in paths:
            dest_path = self.music_dir / src_path.name
            try:
                # Avoid clobbering different songs with the same name
                if dest_path.exists() and dest_path.stat().st_size != src_path.stat().st_size:
                    stem, suf = src_path.stem, src_path.suffix
                    n = 2
                    while dest_path.exists():
                        dest_path = self.music_dir / f"{stem}_{n}{suf}"
                        n += 1
                shutil.copy2(src_path, dest_path)
                lrc_src = src_path.with_suffix(".lrc")
                if lrc_src.exists():
                    shutil.copy2(lrc_src, dest_path.with_suffix(".lrc"))
            except Exception as e:
                print(f"Failed to copy file: {e}")
        self._refresh_playlist()
        
    def _save_settings(self) -> None:
        payload = {
            "music_dir": str(self.music_dir),
            "audio_device_id": self._saved_device_id,
            "audio_auto_switch": bool(self.chk_auto_audio.isChecked()) if hasattr(self, "chk_auto_audio") else self._saved_auto_switch,
            "collapsed": bool(getattr(self, "_collapsed", False)),
            "win_w": int(self._expand_size[0]) if getattr(self, "_collapsed", False) else int(self.width()),
            "win_h": int(self._expand_size[1]) if getattr(self, "_collapsed", False) else int(self.height()),
            "win_x": int(self.x()),
            "win_y": int(self.y()),
        }
        if not getattr(self, "_collapsed", False):
            self._expand_size = (payload["win_w"], payload["win_h"])
        self._cfg = payload
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Failed to save settings: {e}")

    def _on_change_dir_clicked(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(
            self, "选择音乐存储文件夹", str(self.music_dir)
        )
        if dir_path:
            self.music_dir = Path(dir_path)
            self.dl_queue.music_dir = self.music_dir
            self._update_path_display()
            self._save_settings()
            self._refresh_playlist()

    def _split_urls(self, text: str) -> list[str]:
        parts = re.split(r"[\n\r|;，,]+", text or "")
        return [p.strip() for p in parts if p.strip() and p.strip().startswith(("http://", "https://"))]

    def _on_download_clicked(self) -> None:
        raw = self.txt_url.text().strip()
        if not raw:
            self.lbl_dl_status.setText("请先粘贴链接（多个用 | 分隔）。")
            return
        urls = self._split_urls(raw)
        if not urls:
            # allow non-http? still try as single url
            urls = [raw]
        self.dl_queue.music_dir = self.music_dir
        messages = []
        for u in urls:
            messages.append(self.dl_queue.enqueue_url(u, expand_page=True))
        self.lbl_dl_status.setText("；".join(messages[-2:]))
        self.txt_url.clear()
        self._refresh_download_ui()

    def _on_pause_all(self) -> None:
        self.dl_queue.pause_all()
        self.lbl_dl_status.setText("已全部暂停（支持断点续传继续下载）。")
        self._refresh_download_ui()

    def _on_resume_all(self) -> None:
        self.dl_queue.resume_all()
        self.lbl_dl_status.setText("已恢复下载队列。")
        self._refresh_download_ui()

    def _on_add_source(self) -> None:
        # Prefer dedicated source field; fall back to download URL box
        raw = ""
        if hasattr(self, "txt_source_url"):
            raw = self.txt_source_url.text().strip()
        if not raw and hasattr(self, "txt_url"):
            raw = self.txt_url.text().strip()
        urls = self._split_urls(raw) if raw else []
        if not urls and raw:
            urls = [raw]
        if not urls:
            self.lbl_dl_status.setText("请在订阅页输入框填写歌单/页面链接。")
            return
        msgs = [self.dl_queue.add_source(u) for u in urls]
        self.lbl_dl_status.setText("；".join(msgs[-2:]))
        if hasattr(self, "txt_source_url"):
            self.txt_source_url.clear()
        self._refresh_download_ui()

    def _on_scan_now(self) -> None:
        self.lbl_dl_status.setText("正在扫描订阅源…")
        QApplication.processEvents()
        # Run scan in a short-lived QThread to avoid blocking UI
        class _ScanThread(QThread):
            done = pyqtSignal(str)

            def __init__(self, queue: DownloadQueueManager) -> None:
                super().__init__()
                self.queue = queue

            def run(self) -> None:
                try:
                    self.done.emit(self.queue.scan_source_now())
                except Exception as exc:
                    self.done.emit(f"扫描失败：{exc}")

        self._scan_thread = _ScanThread(self.dl_queue)
        self._scan_thread.done.connect(self._on_scan_done)
        self._scan_thread.start()

    def _on_scan_done(self, msg: str) -> None:
        self.lbl_dl_status.setText(msg)
        self._refresh_download_ui()

    def _on_daily_scan_toggled(self, state: int) -> None:
        self.dl_queue.set_daily_scan(state == 2, hour=int(self.spin_scan_hour.value()))

    def _on_scan_hour_changed(self, hour: int) -> None:
        self.dl_queue.set_daily_scan(self.chk_daily.isChecked(), hour=int(hour))

    def _on_workers_changed(self, n: int) -> None:
        self.dl_queue.max_workers = max(1, min(6, int(n)))
        self.dl_queue.save()
        self.lbl_dl_status.setText(f"并行下载数：{self.dl_queue.max_workers}（新任务生效）")

    def _on_clear_finished(self) -> None:
        self.dl_queue.clear_finished()
        self._refresh_download_ui()

    def _on_job_double_clicked(self, item: QListWidgetItem) -> None:
        job_id = item.data(Qt.ItemDataRole.UserRole)
        if not job_id:
            return
        snap = self.dl_queue.snapshot()
        job = next((j for j in snap["jobs"] if j["id"] == job_id), None)
        if not job:
            return
        if job["status"] == "paused":
            self.dl_queue.resume_job(job_id)
        elif job["status"] in {"pending", "downloading"}:
            self.dl_queue.pause_job(job_id)
        self._refresh_download_ui()

    def _on_source_double_clicked(self, item: QListWidgetItem) -> None:
        sid = item.data(Qt.ItemDataRole.UserRole)
        if sid:
            self.lbl_dl_status.setText(self.dl_queue.remove_source(sid))
            self._refresh_download_ui()

    def _on_dl_log(self, msg: str) -> None:
        self.lbl_dl_status.setText(msg)

    def _refresh_download_ui(self) -> None:
        if not hasattr(self, "list_jobs"):
            return
        snap = self.dl_queue.snapshot()
        # jobs
        self.list_jobs.clear()
        status_icon = {
            "pending": "⏳",
            "downloading": "⬇️",
            "paused": "⏸",
            "done": "✅",
            "error": "❌",
            "skipped": "⏭",
        }
        for j in snap["jobs"][-40:]:
            icon = status_icon.get(j["status"], "•")
            prog = f" {j['progress']:.0f}%" if j.get("progress") else ""
            title = j.get("title") or j.get("url", "")[-40:]
            msg = j.get("message") or ""
            label = f"{icon} {title}{prog}"
            if msg:
                label += f" · {msg}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, j["id"])
            item.setToolTip(j.get("url") or "")
            self.list_jobs.addItem(item)
        active = snap.get("active", 0)
        pending = snap.get("pending", 0)
        if not snap["jobs"]:
            # keep last log if any
            pass
        else:
            self.lbl_dl_status.setText(f"下载中 {active} · 排队 {pending} · 共 {len(snap['jobs'])} 任务")
        # sources
        self.list_sources.clear()
        for s in snap["sources"]:
            en = "●" if s.get("enabled") else "○"
            last = s.get("last_status") or s.get("last_scan") or "未扫描"
            item = QListWidgetItem(f"{en} {s.get('label') or s.get('url')} · {last}")
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            item.setToolTip(s.get("url") or "")
            self.list_sources.addItem(item)

    def _refresh_audio_devices(self, select_saved: bool = False) -> None:
        if not hasattr(self, "cmb_device"):
            return
        self.cmb_device.blockSignals(True)
        self.cmb_device.clear()
        self.cmb_device.addItem("系统默认", "")
        try:
            devices = self.engine.list_audio_outputs()
        except Exception:
            devices = []
        for desc, did in devices:
            # store device id as hex string for JSON settings
            key = did.hex() if isinstance(did, (bytes, bytearray)) else str(did)
            self.cmb_device.addItem(desc, key)
        # select
        target = self._saved_device_id if select_saved else ""
        if not target and self.cmb_device.currentIndex() < 0:
            target = ""
        if select_saved and self._saved_device_id:
            for i in range(self.cmb_device.count()):
                if self.cmb_device.itemData(i) == self._saved_device_id:
                    self.cmb_device.setCurrentIndex(i)
                    # apply once
                    try:
                        raw = bytes.fromhex(self._saved_device_id) if self._saved_device_id else None
                        self.engine.set_output_device(raw)
                    except Exception:
                        pass
                    break
        self.cmb_device.blockSignals(False)

    def _on_device_changed(self, idx: int) -> None:
        if idx < 0:
            return
        key = self.cmb_device.itemData(idx) or ""
        self._saved_device_id = str(key)
        try:
            raw = bytes.fromhex(key) if key else None
            msg = self.engine.set_output_device(raw)
            self.lbl_dl_status.setText(msg)
        except Exception as exc:
            self.lbl_dl_status.setText(f"切换设备失败：{exc}")
        self._save_settings()

    def _on_auto_audio_toggled(self, state: int) -> None:
        enabled = state == 2
        self._saved_auto_switch = enabled
        self.engine.set_auto_switch(enabled)
        self._save_settings()
        
    def _on_song_downloaded(self, audio_path: object, lrc_path: object = None) -> None:
        # Refresh the playlist to show the newly downloaded song instantly
        self._refresh_playlist()
        path = Path(audio_path) if audio_path else None
        if path is None:
            return
        # Download completion can happen during app startup.  Do not turn it
        # into playback; the user must explicitly choose Play/double-click.
        if self.current_song_idx == -1 and len(self.playlist) > 0:
            for i, p in enumerate(self.playlist):
                if p.name == path.name:
                    self.list_songs.setCurrentRow(i)
                    break
            
    def _on_song_double_clicked(self, item: QListWidgetItem) -> None:
        row = self.list_songs.row(item)
        self._play_index(row)
        
    def _play_index(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.playlist):
            return
        self.current_song_idx = idx
        self.list_songs.setCurrentRow(idx)
        path = self.playlist[idx]
        
        if self.engine.load_song(path):
            self.engine.play()
            self.slider_seek.setEnabled(True)
            if self.chk_hud.isChecked():
                self.hud.show()
                
    def toggle_play(self) -> None:
        if self.current_song_idx == -1 and len(self.playlist) > 0:
            self._play_index(0)
            return
            
        if self.engine.is_playing():
            self.engine.pause()
        else:
            self.engine.play()
            
    def stop_play(self) -> None:
        self.engine.stop()
        self.hud.update_lyrics("", "")
        
    def play_next(self) -> None:
        if not self.playlist:
            return
        
        if self.play_mode == 2:  # Shuffle
            next_idx = random.randint(0, len(self.playlist) - 1)
        else:
            next_idx = (self.current_song_idx + 1) % len(self.playlist)
        self._play_index(next_idx)
        
    def play_prev(self) -> None:
        if not self.playlist:
            return
            
        if self.play_mode == 2:  # Shuffle
            prev_idx = random.randint(0, len(self.playlist) - 1)
        else:
            prev_idx = (self.current_song_idx - 1) % len(self.playlist)
        self._play_index(prev_idx)
        
    def _on_slider_pressed(self) -> None:
        self.is_dragging_slider = True
        
    def _on_slider_released(self) -> None:
        self.engine.set_position(self.slider_seek.value())
        self.is_dragging_slider = False
        
    def _on_slider_moved(self, value: int) -> None:
        # Visually update time label while dragging without seeking player
        seconds = (value // 1000) % 60
        minutes = (value // 60000) % 60
        self.lbl_time_curr.setText(f"{minutes:02d}:{seconds:02d}")
        
    def _on_volume_changed(self, value: int) -> None:
        self.engine.set_volume(value / 100.0)
        
    def _on_mode_changed(self, idx: int) -> None:
        self.play_mode = idx
        self.hud.update_play_mode(idx)
        
    def _on_color_changed(self, *_args) -> None:
        """Compat alias — some builds connected this name."""
        self._apply_lyric_color()

    def _pick_custom_lyric_color(self) -> None:
        from PyQt6.QtWidgets import QColorDialog

        start = getattr(self, "_custom_lyric_color", None) or self.color_presets.get(
            "自定义", QColor(255, 255, 255)
        )
        color = QColorDialog.getColor(start, self, "自定义歌词颜色")
        if not color.isValid():
            return
        self._custom_lyric_color = color
        self.color_presets["自定义"] = color
        # Switch to 自定义 without double-saving mid-change
        blocked = self.cmb_color.blockSignals(True)
        self.cmb_color.setCurrentText("自定义")
        self.cmb_color.blockSignals(blocked)
        self._apply_lyric_color()

    def _apply_lyric_color(self, *_args) -> None:
        name = self.cmb_color.currentText() if hasattr(self, "cmb_color") else ""
        if name == "自定义":
            color = getattr(self, "_custom_lyric_color", None) or self.color_presets.get(
                "自定义", QColor(255, 255, 255)
            )
        else:
            color = self.color_presets.get(name, QColor(255, 255, 255))
        try:
            self.hud.lbl_curr.text_color = color
            self.hud.update()
        except Exception:
            pass
        if self.state:
            cfg = self.state.setdefault("lyrics_player", {})
            cfg["lyric_color_name"] = name
            cfg["lyric_color_hex"] = color.name(QColor.NameFormat.HexRgb)
            if self.callbacks and hasattr(self.callbacks, "save_state"):
                try:
                    self.callbacks.save_state()
                except Exception:
                    pass
        
    def _on_song_ended(self) -> None:
        if self.play_mode == 1:  # Single loop
            self._play_index(self.current_song_idx)
        else:
            self.play_next()
            
    def _on_lyric_changed(self, current: str, next_line: str) -> None:
        if self.chk_hud.isChecked():
            self.hud.update_lyrics(current, next_line)
            
    def _on_position_changed(self, position_ms: int) -> None:
        if not self.is_dragging_slider:
            self.slider_seek.setValue(position_ms)
            seconds = (position_ms // 1000) % 60
            minutes = (position_ms // 60000) % 60
            self.lbl_time_curr.setText(f"{minutes:02d}:{seconds:02d}")
        
    def _on_duration_changed(self, duration_ms: int) -> None:
        self.slider_seek.setMaximum(duration_ms)
        seconds = (duration_ms // 1000) % 60
        minutes = (duration_ms // 60000) % 60
        self.lbl_time_dur.setText(f"{minutes:02d}:{seconds:02d}")
        
    def _on_playback_state_changed(self, playing: bool) -> None:
        if playing:
            self.btn_play.setText("⏸ 暂停")
        else:
            self.btn_play.setText("▶ 播放")
        self.hud.update_playback_state(playing)
        
    def _on_hud_toggled(self, state: int) -> None:
        # Checked → show desktop lyrics (from software). Unchecked → close.
        visible = state == Qt.CheckState.Checked.value or state == 2
        if visible:
            try:
                self.hud.set_locked(bool(self.chk_lock.isChecked()) if hasattr(self, "chk_lock") else False)
            except Exception:
                pass
            self.hud.show()
            self.hud.raise_()
        else:
            try:
                self.hud.set_locked(False)
            except Exception:
                pass
            self.hud.hide()

    def _on_lock_toggled(self, state: int) -> None:
        # 穿透：歌词仍正常显示，鼠标点击穿透到下方窗口
        locked = state == Qt.CheckState.Checked.value or state == 2
        self.hud.set_locked(locked)
        if locked and self.chk_hud.isChecked() and not self.hud.isVisible():
            self.hud.show()
        
    def _on_open_dir_clicked(self) -> None:
        try:
            os.startfile(str(self.music_dir))
        except Exception as e:
            print(f"Failed to open directory: {e}")
            
    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Drag only via title bar handlers — avoid stealing clicks from lists/sliders
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._save_settings()
        except Exception:
            pass
        super().hideEvent(event)
