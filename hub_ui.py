"""Main window: left nav + right content. Compact home + embedded tools."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QSize, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QGuiApplication, QIcon
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


class _UpdateBridge(QObject):
    """Thread-safe UI callbacks for background update network I/O."""

    check_done = pyqtSignal(object, bool)  # result|Exception, silent_if_latest
    progress = pyqtSignal(str)
    download_done = pyqtSignal(object, object)  # path|None, err|None


class _GDriveBridge(QObject):
    """Thread-safe UI callbacks for Google Drive OAuth / test upload."""

    status = pyqtSignal(str)
    folder_id = pyqtSignal(str)


from screenshot_ui import HotkeyCaptureEdit
from skin import bundle_root
from theme import apply_app_palette, main_window_qss


def ui_icon(name: str) -> Path | None:
    p = bundle_root() / "assets" / "ui" / f"{name}.png"
    return p if p.is_file() else None


class MainWindow(QMainWindow):
    # Sidebar mirrors home categories; 效率办公 stays home-only.
    # Groups: 截图与录屏 / 文件传输 / 媒体与系统 — same 2-level style as transfer.

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setWindowTitle("Desktop Toolkit")
        self.resize(1040, 700)
        self.setMinimumSize(900, 560)
        logo = bundle_root() / "logo.png"
        if logo.exists():
            self.setWindowIcon(QIcon(str(logo)))

        # Prefer primary screen so dual-monitor setups don't lose the hub
        try:
            scr = QGuiApplication.primaryScreen()
            if scr:
                g = scr.availableGeometry()
                self.move(g.left() + 60, g.top() + 40)
        except Exception:
            pass

        # Background-thread → main-thread (do NOT use QTimer from worker threads)
        self._update_bridge = _UpdateBridge(self)
        self._update_bridge.check_done.connect(self._on_update_check_done)
        self._update_bridge.progress.connect(self._on_update_progress)
        self._update_bridge.download_done.connect(self._on_update_download_done)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        side = QFrame(objectName="sideNav")
        side.setFixedWidth(188)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 14, 10, 12)
        sl.setSpacing(3)
        brand = QLabel("Desktop\nToolkit")
        brand.setObjectName("brand")
        brand.setWordWrap(True)
        sl.addWidget(brand)
        sl.addSpacing(8)

        self._nav_btns: dict[str, QPushButton] = {}
        self._nav_group_btns: dict[str, QPushButton] = {}
        self._nav_child_keys: dict[str, list[str]] = {}
        _nav_icon_size = QSize(18, 18)

        def _apply_nav_icon(btn: QPushButton, icon_key: str | None) -> None:
            if not icon_key:
                return
            p = ui_icon(icon_key)
            if not p:
                return
            btn.setIcon(QIcon(str(p)))
            btn.setIconSize(_nav_icon_size)

        def _top(key: str, label: str, icon_key: str | None = None) -> None:
            b = QPushButton(f"  {label}")
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            _apply_nav_icon(b, icon_key)
            b.clicked.connect(lambda _=False, k=key: self.goto(k))
            sl.addWidget(b)
            self._nav_btns[key] = b

        def _group(
            group_key: str,
            label: str,
            children: list[tuple[str, str, object, str]],
            *,
            icon_key: str | None = None,
        ) -> None:
            parent = QPushButton(f"  {label}")
            parent.setObjectName("nav")
            parent.setCheckable(True)
            parent.setCursor(Qt.CursorShape.PointingHandCursor)
            _apply_nav_icon(parent, icon_key)
            first_cb = children[0][2]
            parent.clicked.connect(first_cb)
            sl.addWidget(parent)
            self._nav_group_btns[group_key] = parent
            self._nav_btns[group_key] = parent

            wrap = QWidget()
            wrap_l = QVBoxLayout(wrap)
            wrap_l.setContentsMargins(12, 0, 0, 4)
            wrap_l.setSpacing(2)
            child_keys: list[str] = []
            for ckey, clabel, cb, cicon in children:
                b = QPushButton(f"  {clabel}")
                b.setObjectName("navChild")
                b.setCheckable(True)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                _apply_nav_icon(b, cicon)
                b.clicked.connect(cb)
                wrap_l.addWidget(b)
                self._nav_btns[ckey] = b
                child_keys.append(ckey)
                if ckey == "lan":
                    self.btn_nav_lan = b
                elif ckey == "p2p":
                    self.btn_nav_p2p = b
                elif ckey == "shot":
                    self.btn_nav_shot = b
                elif ckey == "record":
                    self.btn_nav_record = b
                elif ckey == "music":
                    self.btn_nav_music = b
                elif ckey == "clean":
                    self.btn_nav_clean = b
                elif ckey == "organize":
                    self.btn_nav_organize = b
                elif ckey == "uninstall":
                    self.btn_nav_uninstall = b
            self._nav_child_keys[group_key] = child_keys
            if group_key == "transfer":
                self.btn_nav_transfer = parent
                self.transfer_sub = wrap
            elif group_key == "capture":
                self.btn_nav_capture = parent
            elif group_key == "media":
                self.btn_nav_media = parent
            sl.addWidget(wrap)

        _top("home", "首页", "settings")
        _group(
            "capture",
            "截图与录屏",
            [
                ("shot", "截图", lambda: self.goto("shot"), "shot"),
                ("record", "录屏", lambda: self.goto("record"), "recorder"),
            ],
            icon_key="shot",
        )
        _group(
            "transfer",
            "文件传输",
            [
                ("lan", "局域网共享", lambda: self.goto_transfer("lan"), "lan"),
                ("p2p", "跨网传文件", lambda: self.goto_transfer("p2p"), "p2p"),
            ],
            icon_key="lan",
        )
        _group(
            "media",
            "媒体与系统",
            [
                ("music", "音乐播放器", lambda: self.goto("music"), "music"),
                ("clean", "清理", lambda: self.goto("clean"), "clean"),
                ("organize", "文件整理", lambda: self.goto("organize"), "organize"),
                ("uninstall", "卸载软件", lambda: self.goto("uninstall"), "uninstall"),
            ],
            icon_key="music",
        )
        _top("prefs", "偏好设置", "settings")
        sl.addStretch(1)
        try:
            from updater import get_app_version

            ver = QLabel(f"v{get_app_version()}")
        except Exception:
            ver = QLabel("v?")
        ver.setObjectName("muted")
        sl.addWidget(ver)
        outer.addWidget(side)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        self._page_builders = dict(
            (
            ("home", self._page_home),
            ("shot", self._page_shot),
            ("record", self._page_record),
            ("music", self._page_music),
            ("transfer", self._page_transfer),
            ("clean", self._page_clean),
            ("organize", self._page_organize),
            ("uninstall", self._page_uninstall),
            ("prefs", self._page_prefs),
            )
        )
        self._page_keys: list[str] = []
        self._loaded_pages: set[str] = set()
        for key, builder in self._page_builders.items():
            self._page_keys.append(key)
            # Home and preferences are needed by startup services. Expensive media,
            # recorder and transfer widgets are created only when first opened.
            if key in {"home", "prefs"}:
                self.stack.addWidget(builder())
                self._loaded_pages.add(key)
            else:
                self.stack.addWidget(QWidget())

        self.apply_theme()
        self.goto("home")
        # Idle-preload remaining pages so later sidebar clicks feel instant
        QTimer.singleShot(150, self._preload_all_pages)

    def _preload_all_pages(self) -> None:
        pending = [k for k in self._page_keys if k not in self._loaded_pages]
        if not pending:
            return

        def _one() -> None:
            if not pending:
                return
            key = pending.pop(0)
            try:
                self.setUpdatesEnabled(False)
                self._ensure_page(key)
            except Exception:
                pass
            finally:
                self.setUpdatesEnabled(True)
            if pending:
                QTimer.singleShot(80, _one)

        QTimer.singleShot(0, _one)

    def theme_mode(self) -> str:
        return str((self.host.store.state.get("prefs") or {}).get("theme") or "dark")

    def apply_theme(self) -> None:
        mode = self.theme_mode()
        apply_app_palette(self.host.app, mode)
        self.setStyleSheet(main_window_qss(mode))
        if hasattr(self, "lbl_theme_tip"):
            self.lbl_theme_tip.setText(f"当前：{'白天模式' if mode == 'light' else '暗黑模式'}")

    def _sync_nav_checked(self, active: str) -> None:
        """Highlight the active page/child and its category parent."""
        group_of = {
            "shot": "capture",
            "record": "capture",
            "lan": "transfer",
            "p2p": "transfer",
            "transfer": "transfer",
            "music": "media",
            "clean": "media",
            "organize": "media",
            "uninstall": "media",
        }
        active_group = group_of.get(active)
        for k, b in self._nav_btns.items():
            if k in self._nav_group_btns:
                b.setChecked(k == active_group)
            elif k == active:
                b.setChecked(True)
            elif active == "transfer" and k in ("lan", "p2p"):
                # parent-only; leave child selection to goto_transfer
                continue
            else:
                b.setChecked(False)

    def goto(self, key: str) -> None:
        self._sync_nav_checked(key)
        if key not in self._page_keys:
            return
        # Prefer already-built pages; only block paints when first creating a heavy page
        need_build = key not in self._loaded_pages
        if need_build:
            self.setUpdatesEnabled(False)
        try:
            self._ensure_page(key)
            self.stack.setCurrentIndex(self._page_keys.index(key))
        finally:
            if need_build:
                self.setUpdatesEnabled(True)
        QTimer.singleShot(0, lambda: self._warmup_neighbors(key))

    def _ensure_page(self, key: str) -> None:
        if key in self._loaded_pages or key not in self._page_builders:
            return
        index = self._page_keys.index(key)
        placeholder = self.stack.widget(index)
        page = self._page_builders[key]()
        self.stack.removeWidget(placeholder)
        placeholder.deleteLater()
        self.stack.insertWidget(index, page)
        self._loaded_pages.add(key)

    def _warmup_neighbors(self, key: str) -> None:
        """Pre-build adjacent nav pages so switching stays snappy."""
        order = list(self._page_keys)
        try:
            i = order.index(key)
        except ValueError:
            return
        for j in (i - 1, i + 1, i + 2):
            if 0 <= j < len(order):
                try:
                    self._ensure_page(order[j])
                except Exception:
                    pass

    def _home_card(self, icon: str, text: str, cb) -> QPushButton:
        b = QPushButton(f"  {text}")
        b.setObjectName("homeCard")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        b.setMinimumHeight(58)
        b.setMaximumHeight(68)
        p = ui_icon(icon)
        if p:
            b.setIcon(QIcon(str(p)))
            b.setIconSize(QSize(28, 28))
        b.clicked.connect(cb)
        return b

    def _row(self, title: str, cards: list[tuple[str, str, object]]) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 2, 0, 4)
        lay.setSpacing(4)
        sec = QLabel(title)
        sec.setObjectName("section")
        lay.addWidget(sec)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for i, (icon, text, cb) in enumerate(cards):
            grid.addWidget(self._home_card(icon, text, cb), 0, i)
        for c in range(max(4, len(cards))):
            grid.setColumnStretch(c, 1)
        lay.addLayout(grid)
        return wrap

    def _page_home(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        lay.addWidget(QLabel("首页", objectName="pageTitle"))
        tip = QLabel("第一排效率 · 第二排截图录屏 · 第三排传输 · 第四排媒体与系统")
        tip.setObjectName("muted")
        lay.addWidget(tip)
        lay.addWidget(
            self._row(
                "效率办公",
                [
                    ("todos", "待办事项", self.host.show_todos),
                    ("notes", "便签", self.host.show_notes),
                    ("notebook", "笔记本", self.host.show_notebook),
                    ("pomodoro", "番茄钟", self.host.show_pomodoro),
                    ("alarm", "闹钟", self.host.show_alarm_board),
                ],
            )
        )
        lay.addWidget(
            self._row(
                "截图与录屏",
                [
                    ("shot", "截图", lambda: self.goto("shot")),
                    ("recorder", "录屏", lambda: self.goto("record")),
                ],
            )
        )
        lay.addWidget(
            self._row(
                "文件传输",
                [
                    ("lan", "局域网共享", lambda: self.goto_transfer("lan")),
                    ("p2p", "跨网传文件", lambda: self.goto_transfer("p2p")),
                ],
            )
        )
        lay.addWidget(
            self._row(
                "媒体与系统",
                [
                    ("music", "音乐播放器", lambda: self.goto("music")),
                    ("clean", "清理电脑", lambda: self.goto("clean")),
                    ("organize", "文件整理", lambda: self.goto("organize")),
                    ("uninstall", "卸载软件", lambda: self.goto("uninstall")),
                    ("travel", "天气播报", lambda: self.host.announce_weather(force=True)),
                ],
            )
        )
        lay.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def goto_transfer(self, which: str) -> None:
        if which not in ("lan", "p2p"):
            which = "lan"
        self.goto("transfer")
        if which == "lan":
            if hasattr(self, "_show_transfer_lan"):
                self._show_transfer_lan()
            elif hasattr(self, "btn_sub_lan"):
                self.btn_sub_lan.click()
        elif which == "p2p":
            if hasattr(self, "_show_transfer_p2p"):
                self._show_transfer_p2p()
            elif hasattr(self, "btn_sub_p2p"):
                self.btn_sub_p2p.click()
        self._sync_nav_checked(which)

    def _page_shot(self) -> QWidget:
        """Actions fixed on top; settings scroll so small screens stay usable."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(12)
        lay.addWidget(QLabel("截图", objectName="pageTitle"))

        act = QHBoxLayout()
        b1 = QPushButton("区域截图")
        b1.setObjectName("primary")
        b1.setMinimumHeight(36)
        b1.clicked.connect(self.host.start_screenshot_region)
        b2 = QPushButton("全屏截图")
        b2.setObjectName("soft")
        b2.setMinimumHeight(36)
        b2.clicked.connect(self.host.start_screenshot_full)
        act.addWidget(b1)
        act.addWidget(b2)
        act.addStretch(1)
        lay.addLayout(act)
        tip_shot = QLabel("标注时：鼠标滚轮可快速调节画笔粗细（1–40），工具条 +/- 同样可用。")
        tip_shot.setObjectName("muted")
        tip_shot.setWordWrap(True)
        lay.addWidget(tip_shot)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 10px; background: transparent; }"
            "QScrollBar::handle:vertical { background: #475569; border-radius: 5px; min-height: 32px; }"
        )

        panel = QFrame(objectName="panel")
        rl = QVBoxLayout(panel)
        rl.setContentsMargins(14, 12, 14, 16)
        rl.setSpacing(10)
        rl.addWidget(QLabel("截图设置", objectName="pageTitle"))
        cfg = self.host.store.state.setdefault("screenshot", {})
        rl.addWidget(QLabel("保存目录"))
        row = QHBoxLayout()
        self.txt_shot_dir = QLineEdit(
            str(cfg.get("save_dir") or str(Path.home() / "Pictures" / "ToolkitShots"))
        )
        bd = QPushButton("浏览", objectName="soft")
        bd.clicked.connect(self._pick_shot_dir)
        row.addWidget(self.txt_shot_dir, 1)
        row.addWidget(bd)
        rl.addLayout(row)
        rl.addWidget(QLabel("区域快捷键（点框后按键；录入时会暂时释放系统快捷键）"))
        self.hk_region = HotkeyCaptureEdit()
        self.hk_region.setText(str(cfg.get("hotkey_region") or "Ctrl+Alt+A"))
        self.hk_region.hotkey_captured.connect(lambda _c: self._save_shot_settings(True))
        self.hk_region.capture_started.connect(self._pause_shot_hotkeys)
        self.hk_region.capture_ended.connect(self._on_hk_capture_ended)
        rl.addWidget(self.hk_region)
        rl.addWidget(QLabel("全屏快捷键"))
        self.hk_full = HotkeyCaptureEdit()
        self.hk_full.setText(str(cfg.get("hotkey_full") or "Ctrl+Alt+Shift+A"))
        self.hk_full.hotkey_captured.connect(lambda _c: self._save_shot_settings(True))
        self.hk_full.capture_started.connect(self._pause_shot_hotkeys)
        self.hk_full.capture_ended.connect(self._on_hk_capture_ended)
        rl.addWidget(self.hk_full)
        self._hk_applying = False
        g = cfg.setdefault("gdrive", {})
        tip_g = QLabel(
            "云端上传步骤：① 选择 OAuth JSON ② 点「连接 Google」浏览器授权 ③ 保存设置。"
            "只选文件不连接是无法上传的。"
        )
        tip_g.setObjectName("muted")
        tip_g.setWordWrap(True)
        rl.addWidget(tip_g)
        self.chk_gdrive = QCheckBox("启用 Google 云端上传")
        self.chk_gdrive.setChecked(bool(g.get("enabled")))
        rl.addWidget(self.chk_gdrive)
        self.chk_auto_up = QCheckBox("完成后自动上传并复制链接")
        self.chk_auto_up.setChecked(bool(cfg.get("auto_upload")))
        rl.addWidget(self.chk_auto_up)
        rl.addWidget(QLabel("OAuth JSON（桌面应用客户端密钥）"))
        rowg = QHBoxLayout()
        self.txt_secrets = QLineEdit(str(g.get("client_secrets_path") or ""))
        self.txt_secrets.setPlaceholderText("client_secret_xxx.json")
        bg = QPushButton("选择…", objectName="soft")
        bg.clicked.connect(self._pick_secrets)
        rowg.addWidget(self.txt_secrets, 1)
        rowg.addWidget(bg)
        rl.addLayout(rowg)
        self.chk_full_scope = QCheckBox("完整 Drive 权限（可上传到任意已有文件夹）")
        self.chk_full_scope.setChecked(bool(g.get("use_full_scope")))
        rl.addWidget(self.chk_full_scope)
        rl.addWidget(QLabel("目标文件夹 ID（可选，Drive 网址 folders/ 后面）"))
        self.txt_folder = QLineEdit(str(g.get("folder_id") or ""))
        self.txt_folder.setPlaceholderText("不填则自动创建下方文件夹名")
        rl.addWidget(self.txt_folder)
        rl.addWidget(QLabel("自动创建文件夹名（无 ID 时）"))
        self.txt_folder_name = QLineEdit(str(g.get("folder_name") or "桌面工具截图"))
        self.txt_folder_name.setPlaceholderText("桌面工具截图")
        rl.addWidget(self.txt_folder_name)
        grow = QHBoxLayout()
        self.btn_g_connect = QPushButton("连接 Google")
        self.btn_g_connect.setMinimumHeight(34)
        self.btn_g_connect.clicked.connect(self._connect_gdrive)
        self.btn_g_disconnect = QPushButton("断开", objectName="soft")
        self.btn_g_disconnect.setMinimumHeight(34)
        self.btn_g_disconnect.clicked.connect(self._disconnect_gdrive)
        self.btn_g_test = QPushButton("测试上传", objectName="soft")
        self.btn_g_test.setMinimumHeight(34)
        self.btn_g_test.clicked.connect(self._test_gdrive_upload)
        grow.addWidget(self.btn_g_connect)
        grow.addWidget(self.btn_g_disconnect)
        grow.addWidget(self.btn_g_test)
        rl.addLayout(grow)
        self.lbl_gstatus = QLabel("")
        self.lbl_gstatus.setObjectName("muted")
        self.lbl_gstatus.setWordWrap(True)
        rl.addWidget(self.lbl_gstatus)
        save = QPushButton("保存设置", objectName="primary")
        save.setMinimumHeight(38)
        save.clicked.connect(lambda: self._save_shot_settings(True))
        rl.addWidget(save)
        self.lbl_shot_status = QLabel("")
        self.lbl_shot_status.setObjectName("muted")
        self.lbl_shot_status.setWordWrap(True)
        rl.addWidget(self.lbl_shot_status)
        rl.addStretch(1)

        scroll.setWidget(panel)
        lay.addWidget(scroll, 1)
        # GDrive async bridge
        self._gdrive_bridge = _GDriveBridge(self)
        self._gdrive_bridge.status.connect(self._on_gdrive_status)
        self._gdrive_bridge.folder_id.connect(self._on_gdrive_folder_id)
        self._refresh_gdrive_status()
        return page

    def _pick_shot_dir(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "截图目录", self.txt_shot_dir.text())
        if p:
            self.txt_shot_dir.setText(p)

    def _pick_secrets(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "OAuth JSON", "", "JSON (*.json)")
        if p:
            # Copy into app data so later moves of the original file don't break upload
            try:
                import os
                import shutil

                try:
                    from storage import app_data_dir

                    dest_dir = app_data_dir()
                except Exception:
                    dest_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DesktopToolkit"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / "gdrive_client_secrets.json"
                shutil.copy2(p, dest)
                self.txt_secrets.setText(str(dest))
                self.lbl_shot_status.setText(f"已复制密钥到 {dest}，请点「连接 Google」完成授权")
            except Exception:
                self.txt_secrets.setText(p)
            self._save_shot_settings(False)
            self._refresh_gdrive_status()

    def _pause_shot_hotkeys(self) -> None:
        try:
            self.host.pause_screenshot_hotkeys()
        except Exception:
            pass

    def _on_hk_capture_ended(self) -> None:
        if getattr(self, "_hk_applying", False):
            return
        try:
            self.host.resume_screenshot_hotkeys()
        except Exception:
            pass

    def _gdrive_cfg_from_ui(self) -> dict:
        return {
            "enabled": bool(self.chk_gdrive.isChecked()),
            "client_secrets_path": self.txt_secrets.text().strip(),
            "folder_id": self.txt_folder.text().strip(),
            "folder_name": self.txt_folder_name.text().strip() or "桌面工具截图",
            "use_full_scope": bool(self.chk_full_scope.isChecked()),
        }

    def _save_shot_settings(self, apply_hk: bool = False) -> None:
        self._hk_applying = True
        try:
            cfg = self.host.store.state.setdefault("screenshot", {})
            g = cfg.setdefault("gdrive", {})
            cfg["save_dir"] = self.txt_shot_dir.text().strip()
            cfg["hotkey_region"] = self.hk_region.text().strip() or "Ctrl+Alt+A"
            cfg["hotkey_full"] = self.hk_full.text().strip() or "Ctrl+Alt+Shift+A"
            cfg["auto_upload"] = self.chk_auto_up.isChecked()
            g.update(self._gdrive_cfg_from_ui())
            self.host.store.save_state()
            msg = "已保存"
            if apply_hk:
                try:
                    msg = self.host.rebind_screenshot_hotkeys() or msg
                except Exception as e:
                    msg = str(e)
            # Remind if secrets saved but not connected
            try:
                from gdrive_client import GoogleDriveClient

                if g.get("enabled") and g.get("client_secrets_path") and not GoogleDriveClient(g).is_connected():
                    msg += " · 尚未连接 Google，请点「连接 Google」授权"
            except Exception:
                pass
            self.lbl_shot_status.setText(msg)
            self._refresh_gdrive_status()
        finally:
            self._hk_applying = False

    def _refresh_gdrive_status(self) -> None:
        try:
            from gdrive_client import GoogleDriveClient

            gd = self._gdrive_cfg_from_ui()
            client = GoogleDriveClient(gd)
            secrets = gd.get("client_secrets_path") or ""
            if not secrets:
                self.lbl_gstatus.setText("未配置密钥 — 请选择 OAuth JSON")
            elif client.is_connected():
                fid = gd.get("folder_id") or "（自动文件夹）"
                self.lbl_gstatus.setText(f"✅ 已连接 Google · 目标: {fid}")
            else:
                self.lbl_gstatus.setText("密钥已选，但未授权 — 请点「连接 Google」")
        except Exception as e:
            self.lbl_gstatus.setText(f"状态: {e}")

    def _on_gdrive_status(self, msg: str) -> None:
        self.lbl_shot_status.setText(msg)
        self.lbl_gstatus.setText(msg)
        self._refresh_gdrive_status()

    def _on_gdrive_folder_id(self, fid: str) -> None:
        if fid:
            self.txt_folder.setText(fid)
            cfg = self.host.store.state.setdefault("screenshot", {})
            cfg.setdefault("gdrive", {})["folder_id"] = fid
            try:
                self.host.store.save_state()
            except Exception:
                pass

    def _connect_gdrive(self) -> None:
        self._save_shot_settings(False)
        secrets_path = self.txt_secrets.text().strip()
        if not secrets_path:
            self.lbl_shot_status.setText("请先选择 OAuth 客户端 JSON 文件")
            return
        if not Path(secrets_path).is_file():
            self.lbl_shot_status.setText(f"找不到密钥文件: {secrets_path}")
            return
        self.lbl_shot_status.setText("正在授权…请在浏览器中登录 Google")
        self.btn_g_connect.setEnabled(False)

        def work() -> None:
            try:
                from gdrive_client import GoogleDriveClient

                gd = self._gdrive_cfg_from_ui()
                # Persist into store so authorize uses same paths
                self.host.store.state.setdefault("screenshot", {}).setdefault("gdrive", {}).update(gd)
                client = GoogleDriveClient(gd)
                client.authorize_interactive(on_status=lambda m: self._gdrive_bridge.status.emit(m))
                # Mark enabled after successful auth
                gd["enabled"] = True
                try:
                    fid = client.ensure_folder()
                    gd["folder_id"] = str(fid)
                    self._gdrive_bridge.folder_id.emit(str(fid))
                    self._gdrive_bridge.status.emit(f"✅ 已连接，文件夹 ID: {fid}")
                except Exception as e:
                    self._gdrive_bridge.status.emit(f"✅ 已连接（文件夹稍后创建）: {e}")
                self.host.store.state.setdefault("screenshot", {}).setdefault("gdrive", {}).update(gd)
                self.host.store.state["screenshot"]["gdrive"]["enabled"] = True
                try:
                    self.host.store.save_state()
                except Exception:
                    pass
            except Exception as e:
                self._gdrive_bridge.status.emit(f"连接失败: {e}")
            finally:
                from PyQt6.QtCore import QTimer

                QTimer.singleShot(0, lambda: self.btn_g_connect.setEnabled(True))
                QTimer.singleShot(0, lambda: self.chk_gdrive.setChecked(True))

        threading.Thread(target=work, daemon=True).start()

    def _disconnect_gdrive(self) -> None:
        try:
            from gdrive_client import GoogleDriveClient

            GoogleDriveClient(self._gdrive_cfg_from_ui()).clear_token()
            self.lbl_shot_status.setText("已断开 Google 连接")
            self._refresh_gdrive_status()
        except Exception as e:
            self.lbl_shot_status.setText(f"断开失败: {e}")

    def _test_gdrive_upload(self) -> None:
        self._save_shot_settings(False)
        from PyQt6.QtGui import QImage, QColor, QPainter
        import tempfile
        import time

        img = QImage(160, 48, QImage.Format.Format_ARGB32)
        img.fill(QColor(14, 165, 233))
        p = QPainter(img)
        p.setPen(QColor("white"))
        p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "ToolkitShot")
        p.end()
        tmp = Path(tempfile.gettempdir()) / f"toolkit_gdrive_test_{int(time.time())}.png"
        img.save(str(tmp))
        self.lbl_shot_status.setText("正在测试上传…")

        def work() -> None:
            try:
                from gdrive_client import GoogleDriveClient

                gd = self._gdrive_cfg_from_ui()
                client = GoogleDriveClient(gd)
                if not client.is_connected():
                    self._gdrive_bridge.status.emit("测试失败：尚未连接 Google，请先点「连接 Google」")
                    return
                r = client.upload_file(tmp, on_status=lambda m: self._gdrive_bridge.status.emit(m))
                link = r.get("webViewLink") or ""
                self._gdrive_bridge.status.emit(f"测试上传成功 {r.get('name', '')} {link}")
                self.host.store.state.setdefault("screenshot", {}).setdefault("gdrive", {}).update(gd)
                try:
                    self.host.store.save_state()
                except Exception:
                    pass
            except Exception as e:
                self._gdrive_bridge.status.emit(f"测试上传失败: {e}")
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _page_record(self) -> QWidget:
        from recorder_ui import FloatingRecorderBoard

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(QLabel("录屏", objectName="pageTitle"))
        if not getattr(self.host, "_embed_recorder", None):
            self.host._embed_recorder = FloatingRecorderBoard(
                self.host._cb(), self.host.store.state, embedded=True
            )
        lay.addWidget(self.host._embed_recorder, 1)
        return body

    def _page_music(self) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(QLabel("音乐播放器", objectName="pageTitle"))
        # Embed music dashboard
        try:
            from lyrics_engine import LyricsEngine
            from lyrics_ui import FloatingLyricsWindow, LyricsDashboard

            if not getattr(self.host, "_embed_music", None):
                if self.host.lyrics_engine is None:
                    self.host.lyrics_engine = LyricsEngine(self.host)
                if self.host.lyrics_hud is None:
                    self.host.lyrics_hud = FloatingLyricsWindow()
                    self.host.lyrics_hud.hide()
                self.host._embed_music = LyricsDashboard(
                    self.host.lyrics_engine,
                    self.host.lyrics_hud,
                    state=self.host.store.state,
                    callbacks=self.host._cb(),
                    embedded=True,
                )
            lay.addWidget(self.host._embed_music, 1)
        except Exception as e:
            tip = QLabel(f"音乐模块加载失败：{e}")
            tip.setObjectName("muted")
            tip.setWordWrap(True)
            lay.addWidget(tip)
            b = QPushButton("重试打开", objectName="primary")
            b.clicked.connect(self.host.show_music_player)
            lay.addWidget(b)
            lay.addStretch(1)
        return body

    def _page_transfer(self) -> QWidget:
        """Left-nav switches lan/p2p; boards lazy-load. Remote is intentionally omitted."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        self.transfer_stack = QStackedWidget()
        self._transfer_built: set[str] = set()

        def _placeholder(tip: str) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lbl = QLabel(tip)
            lbl.setObjectName("muted")
            lay.addWidget(lbl)
            lay.addStretch(1)
            return w

        self.transfer_stack.addWidget(_placeholder("局域网共享加载中…"))  # 0 lan
        self.transfer_stack.addWidget(_placeholder("跨网传文件加载中…"))  # 1 p2p
        outer.addWidget(self.transfer_stack, 1)

        def _ensure_transfer(which: str) -> None:
            if which in self._transfer_built:
                return
            index = {"lan": 0, "p2p": 1}[which]
            old = self.transfer_stack.widget(index)
            body = QWidget()
            lay = QVBoxLayout(body)
            lay.setContentsMargins(0, 0, 0, 0)
            if which == "lan":
                from lan_ui import FloatingLanBoard

                if not getattr(self.host, "_embed_lan", None):
                    self.host._embed_lan = FloatingLanBoard(self.host, embedded=True)
                lay.addWidget(self.host._embed_lan)
            else:
                from p2p_ui import FloatingP2PBoard

                if not getattr(self.host, "_embed_p2p", None):
                    self.host._embed_p2p = FloatingP2PBoard(
                        self.host._cb(), self.host.store.state, embedded=True
                    )
                lay.addWidget(self.host._embed_p2p)
            self.transfer_stack.removeWidget(old)
            old.deleteLater()
            self.transfer_stack.insertWidget(index, body)
            self.transfer_stack.setCurrentIndex(index)
            self._transfer_built.add(which)

        # Compat stubs so goto_transfer can still "click" switches
        self.btn_sub_lan = QPushButton()
        self.btn_sub_lan.hide()
        self.btn_sub_p2p = QPushButton()
        self.btn_sub_p2p.hide()

        def show_lan() -> None:
            _ensure_transfer("lan")
            self.transfer_stack.setCurrentIndex(0)
            try:
                self.host._embed_lan._refresh_status()
            except Exception:
                pass

        def show_p2p() -> None:
            _ensure_transfer("p2p")
            self.transfer_stack.setCurrentIndex(1)

        self.btn_sub_lan.clicked.connect(show_lan)
        self.btn_sub_p2p.clicked.connect(show_p2p)
        self._show_transfer_lan = show_lan
        self._show_transfer_p2p = show_p2p
        show_lan()
        return page

    def _page_clean(self) -> QWidget:
        from cleaner_ui import FloatingCleanerBoard

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(QLabel("清理", objectName="pageTitle"))
        if not getattr(self.host, "_embed_cleaner", None):
            self.host._embed_cleaner = FloatingCleanerBoard(
                self.host.store.state,
                on_start_clean=self.host.start_deep_clean,
                on_save_state=self.host.store.save_state,
                embedded=True,
            )
        lay.addWidget(self.host._embed_cleaner, 1)
        return body

    def _page_organize(self) -> QWidget:
        from file_organizer_ui import FileOrganizerWindow

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(QLabel("文件整理", objectName="pageTitle"))
        if not getattr(self.host, "_embed_organize", None):
            self.host._embed_organize = FileOrganizerWindow(embedded=True)
        lay.addWidget(self.host._embed_organize, 1)
        return body

    def _page_uninstall(self) -> QWidget:
        from app_uninstaller_ui import AppUninstallerPanel

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(QLabel("卸载软件", objectName="pageTitle"))
        if not getattr(self.host, "_embed_uninstaller", None):
            announce = getattr(self.host, "announce", None)
            self.host._embed_uninstaller = AppUninstallerPanel(
                embedded=True,
                on_announce=announce if callable(announce) else None,
            )
        lay.addWidget(self.host._embed_uninstaller, 1)
        return body

    def _page_prefs(self) -> QWidget:
        page = QWidget()
        page_lay = QVBoxLayout(page)
        page_lay.setContentsMargins(18, 14, 18, 14)
        page_lay.setSpacing(8)
        page_lay.addWidget(QLabel("偏好设置", objectName="pageTitle"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 8, 8)
        lay.setSpacing(10)

        lay.addWidget(QLabel("软件外观", objectName="section"))
        row = QHBoxLayout()
        b1 = QPushButton("暗黑模式", objectName="soft")
        b2 = QPushButton("白天模式", objectName="soft")
        b1.clicked.connect(lambda: self._set_theme("dark"))
        b2.clicked.connect(lambda: self._set_theme("light"))
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addLayout(row)
        self.lbl_theme_tip = QLabel("")
        self.lbl_theme_tip.setObjectName("muted")
        lay.addWidget(self.lbl_theme_tip)

        lay.addWidget(QLabel("启动与桌面助手", objectName="section"))
        prefs = self.host.store.state.setdefault("prefs", {})
        self.chk_autostart = QCheckBox("开机自动启动")
        try:
            from autostart import is_autostart_enabled

            self.chk_autostart.setChecked(bool(is_autostart_enabled() or prefs.get("autostart")))
        except Exception:
            self.chk_autostart.setChecked(bool(prefs.get("autostart")))
        self.chk_autostart.toggled.connect(self._on_autostart)
        lay.addWidget(self.chk_autostart)

        try:
            from float_assistant import (
                default_float_assistant_enabled,
                float_assistant_supported,
            )

            float_default = default_float_assistant_enabled()
            float_ok = float_assistant_supported()
        except Exception:
            float_default = True
            float_ok = True
        self.chk_float_logo = QCheckBox("显示悬浮机器人助手（可拖动，记住位置）")
        self.chk_float_logo.setChecked(
            bool(prefs.get("float_assistant", float_default)) if float_ok else False
        )
        self.chk_float_logo.toggled.connect(self._on_float_logo)
        lay.addWidget(self.chk_float_logo)
        if float_ok:
            tip_a = QLabel(
                "悬停打开快捷菜单；拖到任意位置后会记住。"
                "会定时保持在其它窗口前面；若仍被盖住，可用托盘「找回悬浮机器人」。"
                "独占全屏游戏期间系统可能压过置顶。"
            )
        else:
            tip_a = QLabel(
                "当前系统（macOS / Linux）不显示桌面悬浮机器人，仅保留主窗口与托盘入口。"
            )
            self.chk_float_logo.setEnabled(False)
            self.chk_float_logo.setChecked(False)
        tip_a.setObjectName("muted")
        tip_a.setWordWrap(True)
        lay.addWidget(tip_a)

        lay.addWidget(QLabel("提示与语音播报", objectName="section"))
        self.chk_tips = QCheckBox("显示字幕提示")
        self.chk_tips.setChecked(bool(prefs.get("tips_enabled", True)))
        self.chk_tips.toggled.connect(self._on_tips)
        lay.addWidget(self.chk_tips)
        self.chk_voice = QCheckBox("开启语音播报（Windows 系统语音）")
        self.chk_voice.setChecked(bool(prefs.get("voice_enabled", True)))
        self.chk_voice.toggled.connect(self._on_voice)
        lay.addWidget(self.chk_voice)
        test_row = QHBoxLayout()
        btn_test = QPushButton("试听语音", objectName="soft")
        btn_test.clicked.connect(self._test_voice)
        test_row.addWidget(btn_test)
        test_row.addStretch(1)
        lay.addLayout(test_row)
        self.lbl_prefs_status = QLabel("")
        self.lbl_prefs_status.setObjectName("muted")
        lay.addWidget(self.lbl_prefs_status)

        lay.addWidget(QLabel("快捷键", objectName="section"))
        lay.addWidget(
            QLabel("Ctrl+Alt+T 主窗口 · 截图快捷键在「截图」页设置", objectName="muted")
        )

        lay.addWidget(QLabel("版本与更新", objectName="section"))
        try:
            from updater import get_app_version

            ver_txt = get_app_version()
        except Exception:
            ver_txt = "?"
        self.lbl_app_version = QLabel(f"当前版本：v{ver_txt}")
        self.lbl_app_version.setObjectName("muted")
        lay.addWidget(self.lbl_app_version)
        tip_ver = QLabel(
            "若这里不是最新版，请先卸载旧版或关掉托盘里旧进程，"
            "再从 GitHub Latest 安装包覆盖安装。"
        )
        tip_ver.setObjectName("muted")
        tip_ver.setWordWrap(True)
        lay.addWidget(tip_ver)
        self.chk_auto_update = QCheckBox("启动时自动检查更新（有新版本会提示）")
        self.chk_auto_update.setChecked(bool(self._prefs().get("auto_check_update", True)))
        self.chk_auto_update.toggled.connect(self._on_auto_update)
        lay.addWidget(self.chk_auto_update)
        upd_row = QHBoxLayout()
        self.btn_check_update = QPushButton("检查更新", objectName="primary")
        self.btn_check_update.clicked.connect(self._check_update)
        self.btn_open_release = QPushButton("打开最新下载页", objectName="soft")
        self.btn_open_release.setEnabled(True)
        self.btn_open_release.clicked.connect(self._open_latest_download)
        upd_row.addWidget(self.btn_check_update)
        upd_row.addWidget(self.btn_open_release)
        upd_row.addStretch(1)
        lay.addLayout(upd_row)
        self.lbl_update_status = QLabel("点「检查更新」：有新版本会询问是否下载安装，不会直接打开网页。")
        self.lbl_update_status.setObjectName("muted")
        self.lbl_update_status.setWordWrap(True)
        lay.addWidget(self.lbl_update_status)
        self._last_release_url = ""
        self._last_download_url = ""
        self._last_asset_name = ""
        self._update_checking = False

        lay.addStretch(1)
        scroll.setWidget(body)
        page_lay.addWidget(scroll, 1)
        self.apply_theme()
        return page

    def _prefs(self) -> dict:
        return self.host.store.state.setdefault("prefs", {})

    def _set_theme(self, mode: str) -> None:
        self._prefs()["theme"] = mode
        self.host.store.save_state()
        self.apply_theme()

    def _on_autostart(self, on: bool) -> None:
        self._prefs()["autostart"] = bool(on)
        self.host.store.save_state()
        try:
            from autostart import set_autostart

            msg = set_autostart(bool(on))
        except Exception as e:
            msg = str(e)
        self.lbl_prefs_status.setText(msg)
        try:
            self.host.announce(msg)
        except Exception:
            pass

    def _on_float_logo(self, on: bool) -> None:
        self._prefs()["float_assistant"] = bool(on)
        self.host.store.save_state()
        try:
            self.host.set_float_assistant_visible(bool(on))
        except Exception:
            pass
        self.lbl_prefs_status.setText("已显示悬浮助手" if on else "已隐藏悬浮助手")

    def _on_tips(self, on: bool) -> None:
        self._prefs()["tips_enabled"] = bool(on)
        self.host.store.save_state()

    def _on_voice(self, on: bool) -> None:
        self._prefs()["voice_enabled"] = bool(on)
        self.host.store.save_state()
        self.lbl_prefs_status.setText("语音播报已开启" if on else "语音播报已关闭")

    def _test_voice(self) -> None:
        try:
            self.host.announce("桌面工具箱语音播报正常", force_voice=True)
            self.lbl_prefs_status.setText("正在试听…")
        except Exception as e:
            self.lbl_prefs_status.setText(f"试听失败：{e}")

    def _on_auto_update(self, on: bool) -> None:
        self._prefs()["auto_check_update"] = bool(on)
        self.host.store.save_state()

    def _check_update(self, *, silent_if_latest: bool = False) -> None:
        """Query GitHub; if newer, offer download+install (not just open browser)."""
        if self._update_checking:
            return
        self._update_checking = True
        if hasattr(self, "btn_check_update"):
            self.btn_check_update.setEnabled(False)
        if hasattr(self, "lbl_update_status"):
            self.lbl_update_status.setText("正在检查更新…")
        import threading

        def work() -> None:
            try:
                from updater import check_for_update

                # Slightly longer timeout; always return via signal (never hang UI)
                result = check_for_update(timeout=12.0)
            except Exception as e:
                result = e
            self._update_bridge.check_done.emit(result, bool(silent_if_latest))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_check_done(self, result, silent_if_latest: bool) -> None:
        self._update_checking = False
        if hasattr(self, "btn_check_update"):
            self.btn_check_update.setEnabled(True)
        if isinstance(result, Exception):
            if hasattr(self, "lbl_update_status"):
                self.lbl_update_status.setText(f"检查失败：{result}")
            if not silent_if_latest:
                QMessageBox.warning(self, "检查更新", f"检查失败：{result}")
            return
        self._apply_update_result(result, silent_if_latest=silent_if_latest)

    def _on_update_progress(self, text: str) -> None:
        if hasattr(self, "lbl_update_status"):
            self.lbl_update_status.setText(text)

    def _on_update_download_done(self, path, err) -> None:
        if hasattr(self, "btn_check_update"):
            self.btn_check_update.setEnabled(True)
        if err is not None:
            if hasattr(self, "lbl_update_status"):
                self.lbl_update_status.setText(f"下载/安装失败：{err}")
            QMessageBox.warning(self, "更新失败", str(err))
            return
        if hasattr(self, "lbl_update_status"):
            self.lbl_update_status.setText(f"已启动安装程序：{path}")
        box = QMessageBox(self)
        box.setWindowTitle("更新")
        box.setIcon(QMessageBox.Icon.Information)
        import sys as _sys

        if _sys.platform == "darwin":
            box.setText(
                "Mac 安装包已打开/安装。\n"
                "若是 DMG：请把 DesktopToolkit 拖进「应用程序」，然后退出旧版再打开新版。\n"
                "若已自动复制到「应用程序」：请退出本软件后从启动台打开新版本。"
            )
        else:
            box.setText("安装程序已启动。\n为避免文件占用，建议现在退出 Desktop Toolkit。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.button(QMessageBox.StandardButton.Yes).setText("退出软件")
        box.button(QMessageBox.StandardButton.No).setText("稍后退出")
        if box.exec() == QMessageBox.StandardButton.Yes:
            try:
                self.host.quit()
            except Exception:
                pass

    def _apply_update_result(self, result, *, silent_if_latest: bool = False) -> None:
        self.lbl_update_status.setText(result.message)
        self._last_release_url = result.release_url or ""
        self._last_download_url = result.download_url or ""
        self._last_asset_name = getattr(result, "asset_name", "") or ""
        self._last_sha256 = getattr(result, "sha256_expected", "") or ""
        self._last_sha256_url = getattr(result, "sha256_url", "") or ""
        self.btn_open_release.setEnabled(bool(self._last_release_url or self._last_download_url))
        if not result.ok:
            if not silent_if_latest:
                QMessageBox.warning(self, "检查更新", result.message)
            return
        if not result.has_update:
            if not silent_if_latest:
                QMessageBox.information(self, "检查更新", result.message)
                try:
                    self.host.announce(result.message)
                except Exception:
                    pass
            else:
                self.lbl_update_status.setText(result.message)
            return

        # Has update → ask download & install
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(result.message)
        dl = (self._last_download_url or "").lower()
        an = (self._last_asset_name or "").lower()
        import sys as _sys

        is_mac = _sys.platform == "darwin"
        has_package = bool(self._last_download_url) and (
            dl.endswith(".exe")
            or dl.endswith(".dmg")
            or dl.endswith(".zip")
            or "setup" in an
            or "macos" in an
        )
        if has_package and is_mac:
            box.setInformativeText(
                "是否下载并安装？\n"
                "· DMG：打开后把 DesktopToolkit 拖进「应用程序」\n"
                "· ZIP：会尝试自动复制到「应用程序」\n"
                "（安装前建议先保存工作）"
            )
        elif has_package:
            box.setInformativeText(
                "是否下载安装包并启动安装程序？\n"
                "（安装前建议先保存工作；安装程序启动后可关闭本软件）"
            )
        else:
            box.setInformativeText(
                "未找到本平台安装包链接，可打开下载页手动下载。"
            )
        btn_install = box.addButton("下载并安装", QMessageBox.ButtonRole.AcceptRole)
        btn_page = box.addButton("打开下载页", QMessageBox.ButtonRole.ActionRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_install if has_package else btn_page)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_install and has_package:
            self._download_and_install()
        elif clicked is btn_page:
            self._open_release_page()

    def _download_and_install(self) -> None:
        url = self._last_download_url
        if not url:
            self.lbl_update_status.setText("没有下载地址")
            return
        self.btn_check_update.setEnabled(False)
        self.lbl_update_status.setText("正在下载安装包…")
        import threading

        name = self._last_asset_name or None

        def work() -> None:
            err = None
            path = None
            try:
                from updater import download_update, launch_installer

                def prog(done: int, total: int) -> None:
                    pct = int(done * 100 / total) if total else 0
                    self._update_bridge.progress.emit(f"正在下载安装包… {pct}%")

                path = download_update(
                    url,
                    filename=name,
                    progress_cb=prog,
                    expected_sha256=getattr(self, "_last_sha256", "") or "",
                    sha256_url=getattr(self, "_last_sha256_url", "") or "",
                    require_sha256=True,
                )
                launch_installer(path)
            except Exception as e:
                err = e
            self._update_bridge.download_done.emit(path, err)

        threading.Thread(target=work, daemon=True).start()

    def _open_latest_download(self) -> None:
        """Always open GitHub /releases/latest (not a stale cached URL)."""
        try:
            from updater import RELEASES_PAGE

            QDesktopServices.openUrl(QUrl(RELEASES_PAGE))
        except Exception:
            QDesktopServices.openUrl(
                QUrl("https://github.com/secure-artifacts/DesktopToolkit/releases/latest")
            )

    def _open_release_page(self) -> None:
        url = self._last_release_url
        if not url:
            self._open_latest_download()
            return
        QDesktopServices.openUrl(QUrl(url))

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        """Close button hides to tray instead of destroying the window."""
        event.ignore()
        self.hide()
        try:
            if getattr(self.host, "tray", None):
                from PyQt6.QtWidgets import QSystemTrayIcon

                self.host.tray.showMessage(
                    "Desktop Toolkit",
                    "主界面已隐藏到托盘（进程仍在运行）。双击托盘图标或按 Ctrl+Alt+T 可再打开。",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
        except Exception:
            pass
