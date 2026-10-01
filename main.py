"""Desktop Toolkit — efficiency tools only (no desktop pet)."""

from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from cleaner import CleanReport, DEFAULT_SCOPES, run_deep_clean_async
from float_assistant import FloatingAssistant
from hotkeys import ToolkitHotkeys
from hub_ui import MainWindow
from lan_share import LanShareClient, LanShareServer
from skin import bundle_root
from storage import JsonStore
from theme import apply_app_palette
from voice import SubtitleToast, VoiceService


def _install_exception_hooks() -> Path:
    log_path = Path(__file__).resolve().parent / "DesktopToolkit-error.log"
    if getattr(sys, "frozen", False):
        log_path = Path(sys.executable).resolve().parent / "DesktopToolkit-error.log"

    def _write(msg: str) -> None:
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(msg if msg.endswith("\n") else msg + "\n")
        except OSError:
            pass

    def _hook(et, ev, tb) -> None:
        _write(
            f"\n[{datetime.now().isoformat(timespec='seconds')}]\n"
            + "".join(traceback.format_exception(et, ev, tb))
        )

    sys.excepthook = _hook
    return log_path


class ToolkitApp(QObject):
    clean_finished = pyqtSignal(object)
    weather_ready = pyqtSignal(str)  # marshal worker result → UI thread

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self.store = JsonStore()
        self.lan_server = LanShareServer()
        self.lan_client = LanShareClient()
        self._cleaning = False
        self.main_win: MainWindow | None = None
        self.recorder_board = None
        self.p2p_board = None
        self.lan_board = None
        self.remote_board = None
        self.clean_board = None
        self.alarm_board = None
        self.todo_board = None
        self.notes_ctl = None
        self.notebook_win = None
        self.file_organizer_win = None
        self.pomo_board = None
        self.lyrics_dashboard = None
        self.lyrics_engine = None
        self.lyrics_hud = None

        mode = str((self.store.state.get("prefs") or {}).get("theme") or "dark")
        apply_app_palette(app, mode)

        self.voice = VoiceService(lambda: self.store.state.get("prefs") or {})
        self.subtitle = SubtitleToast()
        self.voice.spoke.connect(self._on_voice_spoke)
        self.assistant: FloatingAssistant | None = None

        self.tray = self._make_tray()
        sc = self.store.state.get("screenshot") or {}
        self.hotkeys = ToolkitHotkeys(
            open_hub=self.show_hub,
            shot_region=self.start_screenshot_region,
            shot_full=self.start_screenshot_full,
            hub_combo="Ctrl+Alt+T",
            region_combo=str(sc.get("hotkey_region") or "Ctrl+Alt+A"),
            full_combo=str(sc.get("hotkey_full") or "Ctrl+Alt+Shift+A"),
        )
        self.clean_finished.connect(self._on_clean_finished)
        self.weather_ready.connect(self._on_weather_ready)
        self.alarm_timer = QTimer(self)
        self.alarm_timer.timeout.connect(self._alarm_tick)
        self.alarm_timer.start(1000)
        self.weather_timer = QTimer(self)
        self.weather_timer.timeout.connect(self._weather_tick)
        self._weather_busy = False
        self.reload_weather_scheduler()
        self.store.append_log("login", "Desktop Toolkit started")
        QTimer.singleShot(200, self.show_hub)
        QTimer.singleShot(400, self._init_assistant)
        QTimer.singleShot(800, lambda: self.announce("桌面工具箱已就绪"))
        # Quiet update check shortly after startup (prefs.auto_check_update)
        QTimer.singleShot(3500, self._startup_check_update)
        QTimer.singleShot(6000, self._weather_boot_announce)

    def _cb(self) -> SimpleNamespace:
        return SimpleNamespace(
            save_state=self.store.save_state,
            rebind_screenshot_hotkeys=self.rebind_screenshot_hotkeys,
            pause_screenshot_hotkeys=self.pause_screenshot_hotkeys,
            resume_screenshot_hotkeys=self.resume_screenshot_hotkeys,
        )

    def _prefs(self) -> dict:
        return self.store.state.setdefault("prefs", {})

    def announce(self, text: str, *, force_voice: bool | None = None) -> None:
        """Show subtitle tip and optional voice broadcast."""
        prefs = self._prefs()
        if prefs.get("tips_enabled", True) or force_voice:
            try:
                if prefs.get("tips_enabled", True):
                    self.subtitle.show_tip(text)
            except Exception:
                pass
        try:
            self.voice.announce(text, force_voice=force_voice)
        except Exception:
            pass

    def _on_voice_spoke(self, text: str) -> None:
        # Subtitle is shown in announce(); avoid extra tray spam here
        return

    def _init_assistant(self) -> None:
        try:
            from float_assistant import default_float_assistant_enabled

            float_default = default_float_assistant_enabled()
        except Exception:
            float_default = True
        if not self._prefs().get("float_assistant", float_default):
            return
        try:
            self.assistant = FloatingAssistant(self)
        except Exception:
            traceback.print_exc()

    def set_float_assistant_visible(self, visible: bool) -> None:
        if visible:
            if self.assistant is None:
                self.assistant = FloatingAssistant(self)
            else:
                self.assistant.bring_to_front()
        elif self.assistant is not None:
            self.assistant.menu.hide()
            self.assistant.hide()

    def bring_float_assistant_front(self) -> None:
        """Tray: ensure floating robot is visible and topmost."""
        self._prefs()["float_assistant"] = True
        try:
            self.store.save_state()
        except Exception:
            pass
        if self.assistant is None:
            self.assistant = FloatingAssistant(self)
        else:
            self.assistant.bring_to_front()
        # Keep prefs checkbox in sync if hub is open
        try:
            if self.main_win and hasattr(self.main_win, "chk_float_logo"):
                self.main_win.chk_float_logo.blockSignals(True)
                self.main_win.chk_float_logo.setChecked(True)
                self.main_win.chk_float_logo.blockSignals(False)
        except Exception:
            pass

    def _make_tray(self) -> QSystemTrayIcon:
        logo = bundle_root() / "logo.png"
        icon = QIcon(str(logo)) if logo.exists() else QIcon()
        tray = QSystemTrayIcon(icon, self.app)
        menu = QMenu()
        for text, fn in (
            ("打开主界面 (Ctrl+Alt+T)", self.show_hub),
            ("找回悬浮机器人", self.bring_float_assistant_front),
            ("区域截图", self.start_screenshot_region),
            ("录屏", self.show_recorder_board),
            ("跨网传文件", self.show_p2p_board),
            ("局域网共享", self.show_lan_share),
            ("待办", self.show_todos),
            ("便签", self.show_notes),
            ("笔记本", self.show_notebook),
            ("文件整理", self.show_file_organizer),
            ("清理电脑", self.start_deep_clean),
            ("音乐", self.show_music_player),
        ):
            a = QAction(text, menu)
            a.triggered.connect(fn)
            menu.addAction(a)
        menu.addSeparator()
        q = QAction("退出", menu)
        q.triggered.connect(self.quit)
        menu.addAction(q)
        tray.setContextMenu(menu)
        tray.setToolTip("Desktop Toolkit")
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason) -> None:
        # Double-click or single left-click both open hub (many users single-click)
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_hub()

    def _hub_target_geometry(self) -> QRect:
        """Primary-screen rect for the main window (never rely on a broken native size)."""
        scr = QGuiApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            w = min(1120, max(960, g.width() - 80))
            h = min(760, max(620, g.height() - 80))
            return QRect(g.left() + 60, g.top() + 40, w, h)
        return QRect(60, 40, 1040, 700)

    def _force_hub_geometry(self, w) -> None:
        """Apply a sane on-screen geometry; fixes 150×39 / hidden ghost windows."""
        try:
            target = self._hub_target_geometry()
            fg = w.frameGeometry()
            tiny = (
                w.width() < 400
                or w.height() < 300
                or fg.width() < 400
                or fg.height() < 300
            )
            scr = QGuiApplication.primaryScreen()
            on_screen = bool(scr and scr.availableGeometry().intersects(fg)) if fg.width() > 0 else False
            # Always place when size is broken / off-screen / never mapped (0×0)
            if tiny or not on_screen:
                w.setMinimumSize(900, 560)
                w.resize(target.width(), target.height())
                w.move(target.x(), target.y())
                w.setGeometry(target)
        except Exception:
            try:
                w.resize(1040, 700)
                w.move(60, 40)
            except Exception:
                pass

    def show_hub(self) -> None:
        """Open / restore the main window in front of floating tools."""
        try:
            if self.main_win is None:
                self.main_win = MainWindow(self)
            # Closed-with-X only hides; recreate if the C++ object was destroyed
            try:
                _ = self.main_win.isVisible()
            except RuntimeError:
                self.main_win = MainWindow(self)
            w = self.main_win
            if w.isMinimized():
                w.showNormal()

            self._force_hub_geometry(w)
            w.setWindowState(w.windowState() & ~Qt.WindowState.WindowMinimized)
            w.show()
            w.showNormal()
            self._force_hub_geometry(w)
            w.raise_()
            w.activateWindow()

            # Windows: restore with EXPLICIT size (do not use SWP_NOSIZE — that can
            # lock a broken ~150×39 native HWND created before layout finishes).
            try:
                if sys.platform == "win32":
                    import ctypes

                    hwnd = int(w.winId())
                    u32 = ctypes.windll.user32
                    target = self._hub_target_geometry()
                    u32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    # HWND_TOPMOST, set pos+size+show (no NOSIZE/NOMOVE)
                    u32.SetWindowPos(
                        hwnd,
                        -1,
                        int(target.x()),
                        int(target.y()),
                        int(target.width()),
                        int(target.height()),
                        0x0040,  # SWP_SHOWWINDOW
                    )
                    u32.SetForegroundWindow(hwnd)

                    def _drop_topmost() -> None:
                        try:
                            u32.SetWindowPos(
                                hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040
                            )  # HWND_NOTOPMOST + NOSIZE/NOMOVE
                        except Exception:
                            pass

                    def _reassert() -> None:
                        try:
                            if self.main_win is None:
                                return
                            mw = self.main_win
                            if mw.width() < 400 or mw.height() < 300 or not mw.isVisible():
                                self._force_hub_geometry(mw)
                                mw.show()
                                mw.showNormal()
                                mw.raise_()
                                t2 = self._hub_target_geometry()
                                u32.SetWindowPos(
                                    int(mw.winId()),
                                    -1,
                                    int(t2.x()),
                                    int(t2.y()),
                                    int(t2.width()),
                                    int(t2.height()),
                                    0x0040,
                                )
                        except Exception:
                            pass

                    QTimer.singleShot(80, _reassert)
                    QTimer.singleShot(400, _reassert)
                    QTimer.singleShot(12000, _drop_topmost)
            except Exception:
                pass
        except Exception as e:
            try:
                self.announce(f"打开主界面失败：{e}", force_voice=False)
            except Exception:
                pass

    def start_screenshot_region(self) -> None:
        from screenshot_app import start_screenshot

        # Pause global shot hotkeys while a capture is pending/open (avoids stacked freezes)
        self.pause_screenshot_hotkeys()

        def _done(_img) -> None:
            self.resume_screenshot_hotkeys()

        start_screenshot(mode="region", state=self.store.state, on_done=_done)

    def start_screenshot_full(self) -> None:
        from screenshot_app import start_screenshot

        self.pause_screenshot_hotkeys()

        def _done(_img) -> None:
            self.resume_screenshot_hotkeys()

        start_screenshot(mode="full", state=self.store.state, on_done=_done)

    def rebind_screenshot_hotkeys(self) -> str:
        sc = self.store.state.setdefault("screenshot", {})
        msg = self.hotkeys.rebind(
            region=str(sc.get("hotkey_region") or "Ctrl+Alt+A"),
            full=str(sc.get("hotkey_full") or "Ctrl+Alt+Shift+A"),
        )
        self.store.save_state()
        return msg

    def _startup_check_update(self) -> None:
        prefs = self.store.state.get("prefs") or {}
        if not prefs.get("auto_check_update", True):
            return
        try:
            if self.main_win is not None and hasattr(self.main_win, "_check_update"):
                self.main_win._check_update(silent_if_latest=True)
        except Exception:
            pass

    def pause_screenshot_hotkeys(self) -> None:
        try:
            self.hotkeys.pause_screenshot_hotkeys()
        except Exception:
            pass

    def resume_screenshot_hotkeys(self) -> None:
        try:
            self.hotkeys.resume_screenshot_hotkeys()
        except Exception:
            pass

    def reload_weather_scheduler(self) -> None:
        """Start/stop periodic weather TTS from prefs."""
        try:
            self.weather_timer.stop()
        except Exception:
            pass
        cfg = self.store.state.get("weather") or {}
        if not cfg.get("enabled"):
            return
        mins = int(cfg.get("interval_min") or 0)
        if mins <= 0:
            return
        self.weather_timer.start(max(1, mins) * 60 * 1000)

    def _weather_boot_announce(self) -> None:
        cfg = self.store.state.get("weather") or {}
        if cfg.get("enabled") and cfg.get("announce_on_start"):
            self.announce_weather(force=True)

    def _weather_tick(self) -> None:
        cfg = self.store.state.get("weather") or {}
        if cfg.get("enabled"):
            self.announce_weather(force=False)

    def announce_weather(self, *, force: bool = False) -> None:
        """Fetch local weather and announce (subtitle + optional voice)."""
        if self._weather_busy:
            # Allow force retry to clear a stuck busy flag from a hung prior request
            if not force:
                return
            self._weather_busy = False
        cfg = dict(self.store.state.setdefault("weather", {}))
        if not force and not cfg.get("enabled"):
            return
        self._weather_busy = True

        def work() -> None:
            msg = ""
            try:
                from weather import fetch_weather

                report = fetch_weather(cfg)
                msg = report.speak_text()
                # Persist last resolved coords for convenience.
                # Do NOT write display labels into location_text — that poisoned
                # auto mode (geocode of "City · Region · Country" always failed).
                try:
                    w = self.store.state.setdefault("weather", {})
                    w["latitude"] = f"{report.latitude:.5f}"
                    w["longitude"] = f"{report.longitude:.5f}"
                    w["last_place"] = report.place
                    self.store.save_state()
                except Exception:
                    pass
            except Exception as e:
                msg = f"天气获取失败：{e}"
            finally:
                self._weather_busy = False
            # Must use signal — QTimer.singleShot from worker thread often never fires
            self.weather_ready.emit(msg)

        threading.Thread(target=work, daemon=True).start()

    def _on_weather_ready(self, msg: str) -> None:
        """UI-thread handler for weather fetch result."""
        try:
            self.announce(msg, force_voice=True)
        except Exception:
            pass
        try:
            if self.main_win is not None and hasattr(self.main_win, "lbl_weather_status"):
                self.main_win.lbl_weather_status.setText(msg)  # type: ignore[union-attr]
        except Exception:
            pass

    def show_recorder_board(self) -> None:
        from recorder_ui import FloatingRecorderBoard

        if self.recorder_board is None:
            self.recorder_board = FloatingRecorderBoard(self._cb(), self.store.state)
        self.recorder_board.show()
        self.recorder_board.raise_()

    def show_p2p_board(self) -> None:
        from p2p_ui import FloatingP2PBoard

        if self.p2p_board is None:
            self.p2p_board = FloatingP2PBoard(self._cb(), self.store.state)
        self.p2p_board.show()
        self.p2p_board.raise_()

    def show_lan_share(self) -> None:
        from lan_ui import FloatingLanBoard

        if self.lan_board is None:
            self.lan_board = FloatingLanBoard(self)
        self.lan_board.show()
        self.lan_board.raise_()
        self.lan_board._refresh_status()

    def show_remote_control(self) -> None:
        """Remote control is withheld from release builds until the feature matures."""
        try:
            self.announce("远程控制仍在开发中，当前版本暂未开放")
        except Exception:
            pass

    def start_deep_clean(self, scopes: list[str] | None = None) -> None:
        if self._cleaning:
            self.announce("正在清理中，请稍候")
            self.tray.showMessage("清理", "正在清理中…", QSystemTrayIcon.MessageIcon.Warning, 3000)
            return
        self._cleaning = True
        use = list(scopes) if scopes else list(
            (self.store.state.get("cleaner") or {}).get("scopes") or DEFAULT_SCOPES
        )
        self.announce("开始清理电脑")
        self.tray.showMessage("清理", "开始清理电脑…", QSystemTrayIcon.MessageIcon.Information, 3000)

        def done(report: CleanReport) -> None:
            self.clean_finished.emit(report)

        run_deep_clean_async(done, scopes=use)

    def show_cleaner_board(self) -> None:
        """Open main window clean page (embedded settings)."""
        self.show_hub()
        if self.main_win:
            self.main_win.goto("clean")

    def _on_clean_finished(self, report: object) -> None:
        self._cleaning = False
        if isinstance(report, CleanReport):
            summary = report.summary()
            self.announce(f"清理完成。{summary}")
            self.tray.showMessage("清理完成", summary, QSystemTrayIcon.MessageIcon.Information, 6000)
            self.store.append_log("clean_done", summary)
            try:
                emb = getattr(self, "_embed_cleaner", None)
                if emb:
                    emb.on_clean_result(summary)
            except Exception:
                pass

    def show_music_player(self) -> None:
        try:
            from lyrics_engine import LyricsEngine
            from lyrics_ui import FloatingLyricsWindow, LyricsDashboard

            if self.lyrics_dashboard is None:
                self.lyrics_engine = LyricsEngine(self)
                self.lyrics_hud = FloatingLyricsWindow()
                self.lyrics_dashboard = LyricsDashboard(
                    self.lyrics_engine,
                    self.lyrics_hud,
                    state=self.store.state,
                    callbacks=self._cb(),
                )
            self.lyrics_dashboard.show()
            self.lyrics_dashboard.raise_()
        except Exception as e:
            traceback.print_exc()
            QMessageBox.warning(None, "音乐", f"打开失败：{e}")

    def show_pomodoro(self) -> None:
        from simple_boards import PomodoroBoard, _clamp_widget_to_screens

        if self.pomo_board is None:
            self.pomo_board = PomodoroBoard(self.store.state, self.store.save_state)
            ui = (self.store.state.get("pomodoro_ui") or {})
            if ui.get("x") is not None and ui.get("y") is not None:
                try:
                    self.pomo_board.move(int(ui["x"]), int(ui["y"]))
                except (TypeError, ValueError):
                    pass
        _clamp_widget_to_screens(self.pomo_board, min_w=300, min_h=300)
        self.pomo_board.show()
        self.pomo_board.raise_()

    def show_todos(self) -> None:
        from simple_boards import TodosController

        if self.todo_board is None:
            # todo_board holds TodosController (multi-window)
            self.todo_board = TodosController(self.store.state, self.store.save_state)
        self.todo_board.show_all()

    def show_notes(self) -> None:
        from simple_boards import NotesController

        if self.notes_ctl is None:
            self.notes_ctl = NotesController(self.store.state, self.store.save_state)
        self.notes_ctl.show_all()

    def add_note(self) -> None:
        from simple_boards import NotesController

        if self.notes_ctl is None:
            self.notes_ctl = NotesController(self.store.state, self.store.save_state)
        self.notes_ctl.add_note()

    def show_notebook(self) -> None:
        """Open Evernote-style notebook library (independent from sticky notes)."""
        from notebook_ui import show_notebook_window

        show_notebook_window(self, app_name="DesktopToolkit")

    def show_file_organizer(self) -> None:
        """Open file organizer in hub when possible; else floating window."""
        try:
            self.show_hub()
            if self.main_win is not None:
                self.main_win.goto("organize")
                return
        except Exception:
            pass
        from file_organizer_ui import show_file_organizer

        show_file_organizer(self)

    def show_alarm_board(self) -> None:
        from alarm_ui import FloatingAlarmBoard

        if self.alarm_board is None:
            self.alarm_board = FloatingAlarmBoard(self._cb(), self.store.state)
        self.alarm_board.show()
        self.alarm_board.raise_()

    def _alarm_tick(self) -> None:
        import datetime as dt

        now = dt.datetime.now()
        t_cfg = self.store.state.setdefault("timer", {})
        if t_cfg.get("active") and int(t_cfg.get("remaining") or 0) > 0 and not t_cfg.get("paused"):
            t_cfg["remaining"] = int(t_cfg.get("remaining") or 0) - 1
            if t_cfg["remaining"] <= 0:
                t_cfg["active"] = False
                try:
                    from alarm_sounds import play_ringtone

                    play_ringtone(str(t_cfg.get("ringtone") or "beep"))
                except Exception:
                    pass
                name = t_cfg.get("name") or "时间到"
                self.announce(f"倒计时结束。{name}")
                self.tray.showMessage(
                    "倒计时",
                    name,
                    QSystemTrayIcon.MessageIcon.Information,
                    6000,
                )
                self.store.save_state()

        cur = now.strftime("%H:%M")
        day = now.strftime("%Y-%m-%d")
        for alarm in self.store.state.get("alarms") or []:
            if not alarm.get("enabled"):
                continue
            if alarm.get("time") == cur and alarm.get("last_triggered_date") != day:
                alarm["last_triggered_date"] = day
                if alarm.get("repeat") == "once":
                    alarm["enabled"] = False
                try:
                    from alarm_sounds import play_ringtone

                    play_ringtone(str(alarm.get("ringtone") or "beep"))
                except Exception:
                    pass
                aname = str(alarm.get("name") or "闹钟")
                self.announce(f"闹钟提醒。{aname}")
                self.tray.showMessage(
                    "闹钟",
                    aname,
                    QSystemTrayIcon.MessageIcon.Information,
                    8000,
                )
                self.store.save_state()

    def quit(self) -> None:
        try:
            self.hotkeys.close()
        except Exception:
            pass
        try:
            self.lan_server.stop()
        except Exception:
            pass
        try:
            if self.assistant:
                self.assistant.menu.hide()
                self.assistant.hide()
        except Exception:
            pass
        self.store.save_state()
        self.app.quit()


def main() -> int:
    _install_exception_hooks()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setApplicationName("Desktop Toolkit")
    # Single-instance: a second launch just activates the first
    try:
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket

        key = "DesktopToolkit-single-instance"
        probe = QLocalSocket()
        probe.connectToServer(key)
        if probe.waitForConnected(200):
            probe.write(b"raise")
            probe.waitForBytesWritten(500)
            probe.disconnectFromServer()
            return 0
        try:
            QLocalServer.removeServer(key)
        except Exception:
            pass
        server = QLocalServer()
        server.listen(key)
        app._toolkit_instance_server = server  # type: ignore[attr-defined]
    except Exception:
        server = None  # type: ignore[assignment]

    logo = bundle_root() / "logo.png"
    if logo.exists():
        app.setWindowIcon(QIcon(str(logo)))
    try:
        host = ToolkitApp(app)
        if server is not None:

            def _on_second_instance() -> None:
                try:
                    sock = server.nextPendingConnection()
                    if sock:
                        sock.readAll()
                        sock.disconnectFromServer()
                except Exception:
                    pass
                try:
                    host.show_hub()
                except Exception:
                    pass

            server.newConnection.connect(_on_second_instance)
    except Exception as exc:
        traceback.print_exc()
        QMessageBox.critical(None, "Desktop Toolkit", f"启动失败：{exc}")
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
