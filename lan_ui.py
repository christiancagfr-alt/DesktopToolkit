"""LAN share board: host/client, remote browser, profiles, activity log, async download."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QPoint, QObject, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QMouseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lan_share import local_ipv4_addresses


def _fmt_size(n: int | float | None) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 ** 3):.2f} GB"


class _LanBridge(QObject):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)  # cur, total, name
    download_done = pyqtSignal(str)
    list_done = pyqtSignal(object, object, str)  # err|None, entries, path_show


class FloatingLanBoard(QWidget):
    def __init__(self, host, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self.host = host
        self.embedded = embedded
        self.dragging = False
        self.drag_pos = QPoint()
        self._remote_path = ""
        self._downloading = False
        self._bridge = _LanBridge(self)
        self._bridge.log.connect(self._append_log)
        self._bridge.status.connect(self._set_status)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.download_done.connect(self._on_download_done)
        self._bridge.list_done.connect(self._on_list_done)

        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(720, 560)
        self.setMinimumSize(400, 320)
        self._build()
        self._load_prefs()
        self._refresh_status()

    # ---- state helpers ----
    def _cfg(self) -> dict:
        store = getattr(self.host, "store", None)
        state = getattr(store, "state", None) if store else None
        if not isinstance(state, dict):
            state = {}
        return state.setdefault("lan_share", {})

    def _save_state(self) -> None:
        try:
            store = getattr(self.host, "store", None)
            if store and hasattr(store, "save_state"):
                store.save_state()
        except Exception:
            pass

    def _dialog_parent(self) -> QWidget:
        w = self.window()
        return w if isinstance(w, QWidget) else self

    # ---- UI ----
    def _build(self) -> None:
        self.setStyleSheet(
            """
            QFrame#box {
                background: #0f172a; border: 1px solid rgba(99,102,241,0.5);
                border-radius: 16px;
            }
            QFrame#settingsBox {
                background: #111827; border: 1px solid #1e293b; border-radius: 10px;
            }
            QLabel { color: #e2e8f0; font-weight: 600; font-size: 12px; }
            QLabel#title { color: #a5b4fc; font-size: 15px; font-weight: 800; }
            QLabel#muted { color: #94a3b8; font-size: 11px; font-weight: 500; }
            QLabel#section {
                color: #94a3b8; font-size: 11px; font-weight: 800;
                padding-top: 2px;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #020617; color: #f8fafc; border: 1px solid #334155;
                border-radius: 8px; padding: 5px 8px; min-height: 22px; font-size: 12px;
            }
            QPushButton {
                background: #6366f1; color: white; border: none; border-radius: 8px;
                padding: 6px 10px; font-weight: 700; font-size: 12px;
            }
            QPushButton#soft { background: #1e293b; border: 1px solid #334155; }
            QPushButton#danger { background: #dc2626; }
            QTreeWidget {
                background: #020617; color: #e2e8f0; border: 1px solid #334155;
                border-radius: 10px; font-size: 12px; outline: none;
            }
            QTreeWidget::item { padding: 4px 6px; border-radius: 4px; }
            QTreeWidget::item:selected { background: #4338ca; color: white; }
            QHeaderView::section {
                background: #1e293b; color: #a5b4fc; border: none;
                padding: 6px; font-weight: 700;
            }
            QPlainTextEdit {
                background: #020617; color: #94a3b8; border: 1px solid #334155;
                border-radius: 8px; font-size: 11px; font-family: Consolas, monospace;
            }
            QProgressBar {
                background: #1e293b; border: none; border-radius: 6px; height: 14px;
                text-align: center; color: #e2e8f0; font-size: 10px;
            }
            QProgressBar::chunk { background: #6366f1; border-radius: 6px; }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame(objectName="box")
        if self.embedded:
            box.setStyleSheet(
                "QFrame#box { background: transparent; border: none; border-radius: 0; }"
            )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4 if self.embedded else 12, 4 if self.embedded else 10, 4 if self.embedded else 12, 8)
        lay.setSpacing(6)

        if not self.embedded:
            head = QHBoxLayout()
            head.addWidget(QLabel("局域网文件共享", objectName="title"), 1)
            x = QPushButton("×", objectName="soft")
            x.setFixedSize(28, 28)
            x.clicked.connect(self.hide)
            head.addWidget(x)
            lay.addLayout(head)
        else:
            lay.addWidget(QLabel("局域网文件共享", objectName="title"))

        settings = QFrame(objectName="settingsBox")
        s = QVBoxLayout(settings)
        s.setContentsMargins(10, 8, 10, 8)
        s.setSpacing(5)

        self.lbl_ips = QLabel()
        self.lbl_ips.setObjectName("muted")
        self.lbl_ips.setWordWrap(True)
        s.addWidget(self.lbl_ips)

        # ---- Profiles ----
        s.addWidget(QLabel("连接配置（可保存 / 切换）", objectName="section"))
        prow = QHBoxLayout()
        prow.setSpacing(6)
        self.cmb_profile = QComboBox()
        self.cmb_profile.setMinimumHeight(28)
        self.cmb_profile.currentIndexChanged.connect(self._on_profile_picked)
        self.txt_profile_name = QLineEdit()
        self.txt_profile_name.setPlaceholderText("配置名称")
        self.txt_profile_name.setMaximumWidth(140)
        self.txt_profile_name.setMaximumHeight(28)
        self.btn_save_profile = QPushButton("保存配置", objectName="soft")
        self.btn_save_profile.setFixedHeight(28)
        self.btn_save_profile.clicked.connect(self._save_profile)
        self.btn_del_profile = QPushButton("删除", objectName="soft")
        self.btn_del_profile.setFixedHeight(28)
        self.btn_del_profile.clicked.connect(self._delete_profile)
        prow.addWidget(self.cmb_profile, 2)
        prow.addWidget(self.txt_profile_name, 1)
        prow.addWidget(self.btn_save_profile)
        prow.addWidget(self.btn_del_profile)
        s.addLayout(prow)

        s.addWidget(QLabel("主机 · 分享目录", objectName="section"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.txt_root = QLineEdit(str(Path.home() / "Documents"))
        self.txt_root.setMaximumHeight(28)
        b = QPushButton("浏览", objectName="soft")
        b.setFixedHeight(28)
        b.clicked.connect(self._pick)
        row.addWidget(self.txt_root, 1)
        row.addWidget(b)
        s.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("密码"))
        self.txt_pwd = QLineEdit("tool1234")
        self.txt_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_pwd.setMaximumWidth(140)
        self.txt_pwd.setMaximumHeight(28)
        row2.addWidget(self.txt_pwd)
        row2.addWidget(QLabel("端口"))
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.setValue(8765)
        self.spin_port.setMaximumWidth(90)
        self.spin_port.setMaximumHeight(28)
        row2.addWidget(self.spin_port)
        self.btn_start = QPushButton("启动主机")
        self.btn_start.setFixedHeight(28)
        self.btn_start.clicked.connect(self._start_host)
        self.btn_stop = QPushButton("停止", objectName="danger")
        self.btn_stop.setFixedHeight(28)
        self.btn_stop.clicked.connect(self._stop_host)
        self.btn_copy_link = QPushButton("复制链接", objectName="soft")
        self.btn_copy_link.setFixedHeight(28)
        self.btn_copy_link.setToolTip("复制本机分享地址，方便发给同一局域网的对方")
        self.btn_copy_link.clicked.connect(self._copy_share_link)
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
        row2.addWidget(self.btn_copy_link)
        row2.addStretch(1)
        s.addLayout(row2)

        s.addWidget(QLabel("客户端 · 连接远端", objectName="section"))
        row4 = QHBoxLayout()
        row4.setSpacing(6)
        self.txt_host = QLineEdit()
        self.txt_host.setPlaceholderText("对方 IP")
        self.txt_host.setMaximumHeight(28)
        self.txt_client_pwd = QLineEdit()
        self.txt_client_pwd.setPlaceholderText("访问密码")
        self.txt_client_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_client_pwd.setMaximumWidth(140)
        self.txt_client_pwd.setMaximumHeight(28)
        self.txt_client_pwd.setText("tool1234")
        self.btn_conn = QPushButton("连接", objectName="soft")
        self.btn_conn.setFixedHeight(28)
        self.btn_conn.clicked.connect(self._connect)
        self.btn_disc = QPushButton("断开", objectName="soft")
        self.btn_disc.setFixedHeight(28)
        self.btn_disc.clicked.connect(self._disconnect)
        row4.addWidget(self.txt_host, 2)
        row4.addWidget(self.txt_client_pwd, 1)
        row4.addWidget(self.btn_conn)
        row4.addWidget(self.btn_disc)
        s.addLayout(row4)

        self.lbl_status = QLabel("就绪")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        s.addWidget(self.lbl_status)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setMaximumHeight(280)
        settings_scroll.setWidget(settings)
        lay.addWidget(settings_scroll)

        file_head = QHBoxLayout()
        file_head.addWidget(QLabel("远端文件列表", objectName="section"), 1)
        self.lbl_path = QLabel("未连接")
        self.lbl_path.setObjectName("muted")
        file_head.addWidget(self.lbl_path, 2)
        self.btn_up = QPushButton("上级", objectName="soft")
        self.btn_up.setFixedHeight(26)
        self.btn_up.clicked.connect(self._go_up)
        self.btn_refresh = QPushButton("刷新", objectName="soft")
        self.btn_refresh.setFixedHeight(26)
        self.btn_refresh.clicked.connect(self._refresh_list)
        self.btn_download = QPushButton("下载选中")
        self.btn_download.setFixedHeight(26)
        self.btn_download.setToolTip("可多选文件；文件夹会整夹下载。后台进行，进度见下方日志。")
        self.btn_download.clicked.connect(self._download_selected)
        file_head.addWidget(self.btn_up)
        file_head.addWidget(self.btn_refresh)
        file_head.addWidget(self.btn_download)
        lay.addLayout(file_head)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "类型", "大小"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._on_item_dbl)
        lay.addWidget(self.tree, 1)

        dest_row = QHBoxLayout()
        dest_row.setSpacing(6)
        dest_row.addWidget(QLabel("保存到", objectName="section"))
        self.lbl_download_dir = QLabel("")
        self.lbl_download_dir.setObjectName("muted")
        self.lbl_download_dir.setWordWrap(True)
        dest_row.addWidget(self.lbl_download_dir, 1)
        self.btn_pick_dest = QPushButton("更改目录", objectName="soft")
        self.btn_pick_dest.setFixedHeight(26)
        self.btn_pick_dest.clicked.connect(self._change_download_dir)
        self.btn_open_dest = QPushButton("打开目录", objectName="soft")
        self.btn_open_dest.setFixedHeight(26)
        self.btn_open_dest.clicked.connect(self._open_download_dir)
        dest_row.addWidget(self.btn_pick_dest)
        dest_row.addWidget(self.btn_open_dest)
        lay.addLayout(dest_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("活动日志", objectName="section"), 1)
        btn_clear = QPushButton("清空", objectName="soft")
        btn_clear.setFixedHeight(24)
        btn_clear.clicked.connect(lambda: self.log_view.clear())
        log_head.addWidget(btn_clear)
        lay.addLayout(log_head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(90)
        self.log_view.setPlaceholderText("连接、列表、下载过程会显示在这里…")
        lay.addWidget(self.log_view)

        root.addWidget(box)

    # ---- prefs / profiles ----
    def _load_prefs(self) -> None:
        cfg = self._cfg()
        if cfg.get("share_root"):
            self.txt_root.setText(str(cfg["share_root"]))
        if cfg.get("host_password"):
            self.txt_pwd.setText(str(cfg["host_password"]))
        try:
            self.spin_port.setValue(int(cfg.get("port") or 8765))
        except Exception:
            pass
        if cfg.get("client_host"):
            self.txt_host.setText(str(cfg["client_host"]))
        if cfg.get("client_password"):
            self.txt_client_pwd.setText(str(cfg["client_password"]))
        if cfg.get("download_dir"):
            self._download_dir = str(cfg["download_dir"])
        else:
            self._download_dir = str(Path.home() / "Downloads")
        self._update_download_dir_label()
        self._reload_profile_combo(select=str(cfg.get("active_profile") or ""))

    def _persist_fields(self) -> None:
        cfg = self._cfg()
        cfg["share_root"] = self.txt_root.text().strip()
        cfg["host_password"] = self.txt_pwd.text()
        cfg["port"] = int(self.spin_port.value())
        cfg["client_host"] = self.txt_host.text().strip()
        cfg["client_password"] = self.txt_client_pwd.text()
        cfg["download_dir"] = getattr(self, "_download_dir", str(Path.home() / "Downloads"))
        self._save_state()

    def _profiles(self) -> list[dict]:
        raw = self._cfg().get("profiles")
        return list(raw) if isinstance(raw, list) else []

    def _set_profiles(self, profiles: list[dict]) -> None:
        self._cfg()["profiles"] = profiles
        self._save_state()

    def _reload_profile_combo(self, select: str = "") -> None:
        self.cmb_profile.blockSignals(True)
        self.cmb_profile.clear()
        self.cmb_profile.addItem("（当前未保存的连接）", "")
        for p in self._profiles():
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            host = str(p.get("host") or "")
            label = f"{name}  ·  {host}:{p.get('port') or 8765}" if host else name
            self.cmb_profile.addItem(label, name)
        if select:
            idx = self.cmb_profile.findData(select)
            if idx >= 0:
                self.cmb_profile.setCurrentIndex(idx)
        self.cmb_profile.blockSignals(False)

    def _on_profile_picked(self, *_args) -> None:
        name = self.cmb_profile.currentData()
        if not name:
            return
        for p in self._profiles():
            if str(p.get("name") or "") == name:
                self.txt_profile_name.setText(name)
                if p.get("host"):
                    self.txt_host.setText(str(p["host"]))
                if p.get("password") is not None:
                    self.txt_client_pwd.setText(str(p.get("password") or ""))
                try:
                    self.spin_port.setValue(int(p.get("port") or 8765))
                except Exception:
                    pass
                if p.get("share_root"):
                    self.txt_root.setText(str(p["share_root"]))
                if p.get("host_password") is not None:
                    self.txt_pwd.setText(str(p.get("host_password") or ""))
                self._cfg()["active_profile"] = name
                self._persist_fields()
                self._append_log(f"已切换配置「{name}」")
                return

    def _save_profile(self) -> None:
        name = self.txt_profile_name.text().strip() or self.txt_host.text().strip() or "默认"
        profile = {
            "name": name,
            "host": self.txt_host.text().strip(),
            "port": int(self.spin_port.value()),
            "password": self.txt_client_pwd.text(),
            "share_root": self.txt_root.text().strip(),
            "host_password": self.txt_pwd.text(),
        }
        profiles = [p for p in self._profiles() if str(p.get("name") or "") != name]
        profiles.insert(0, profile)
        self._set_profiles(profiles)
        self._cfg()["active_profile"] = name
        self._persist_fields()
        self._reload_profile_combo(select=name)
        self._append_log(f"已保存配置「{name}」")
        self._set_status(f"配置已保存：{name}")

    def _delete_profile(self) -> None:
        name = self.cmb_profile.currentData() or self.txt_profile_name.text().strip()
        if not name:
            self._set_status("没有可删除的配置")
            return
        profiles = [p for p in self._profiles() if str(p.get("name") or "") != name]
        self._set_profiles(profiles)
        if self._cfg().get("active_profile") == name:
            self._cfg()["active_profile"] = ""
        self._reload_profile_combo()
        self.txt_profile_name.clear()
        self._append_log(f"已删除配置「{name}」")

    # ---- logging / status ----
    def _append_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {msg}")
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def _set_status(self, msg: str) -> None:
        self.lbl_status.setText(msg)

    def _refresh_status(self) -> None:
        ips = local_ipv4_addresses() or []
        self.lbl_ips.setText("本机 IP: " + (", ".join(ips) if ips else "未知"))
        try:
            st = self.host.lan_server.status_text()
        except Exception:
            st = ""
        client = self.host.lan_client
        if getattr(client, "connected", False):
            extra = f" · 已连 {client.connected_name}（{client.remote_root_name}）"
        else:
            extra = " · 客户端未连接"
        self.lbl_status.setText((st or "主机未启动") + extra)

    # ---- host / client ----
    def _pick(self) -> None:
        p = QFileDialog.getExistingDirectory(
            self._dialog_parent(), "分享目录", self.txt_root.text()
        )
        if p:
            self.txt_root.setText(p)
            self._persist_fields()

    def _start_host(self) -> None:
        self._persist_fields()
        msg = self.host.lan_server.start(
            self.txt_root.text().strip(),
            self.txt_pwd.text().strip() or "tool1234",
            int(self.spin_port.value()),
        )
        self._set_status(msg)
        self._append_log(msg)
        self._refresh_status()

    def _stop_host(self) -> None:
        msg = self.host.lan_server.stop()
        msg = msg or "已停止"
        self._set_status(msg)
        self._append_log(msg)
        self._refresh_status()

    def _connect(self) -> None:
        host = self.txt_host.text().strip()
        if not host:
            self._set_status("请填写主机 IP")
            self._append_log("连接失败：未填写主机 IP")
            return
        # 127.0.0.1 often refused on Windows when server binds 0.0.0.0 — rewrite to LAN IP
        if host in ("127.0.0.1", "localhost"):
            ips = [ip for ip in (local_ipv4_addresses() or []) if not ip.startswith("127.")]
            if ips:
                self._append_log(f"本机回环地址可能连不上，改用局域网 IP：{ips[0]}")
                host = ips[0]
                self.txt_host.setText(host)
        pwd = self.txt_client_pwd.text().strip()
        if not pwd:
            self._set_status("请填写访问密码")
            self._append_log("连接失败：未填写访问密码")
            return
        self._persist_fields()
        self._append_log(f"正在连接 {host}:{int(self.spin_port.value())} …")
        msg = self.host.lan_client.connect(host, int(self.spin_port.value()), pwd)
        self._set_status(msg)
        self._append_log(msg)
        self._refresh_status()
        if self.host.lan_client.connected:
            self._remote_path = ""
            self._refresh_list()
        elif "无法访问" in msg or "timed out" in msg.lower() or "拒绝" in msg:
            ips = ", ".join(local_ipv4_addresses() or [])
            self._append_log(f"排查：确认对方已点「启动主机」；本机 IP 供对方填：{ips}")

    def _disconnect(self) -> None:
        try:
            self.host.lan_client.disconnect()
            msg = "已断开"
        except Exception as e:
            msg = str(e)
        self._set_status(msg)
        self._append_log(msg)
        self._remote_path = ""
        self.tree.clear()
        self.lbl_path.setText("未连接")
        self._refresh_status()

    def _refresh_list(self) -> None:
        client = self.host.lan_client
        self.tree.clear()
        if not getattr(client, "connected", False):
            self.lbl_path.setText("未连接 — 请先填写 IP 和密码后连接")
            return
        path = self._remote_path
        self.lbl_path.setText("加载中…")
        self._append_log(f"列出目录：{path or '/'}")

        def work() -> None:
            err, entries = client.list_dir(path)
            self._bridge.list_done.emit(err, entries, path or "/")

        threading.Thread(target=work, daemon=True).start()

    def _on_list_done(self, err, entries, path_show: str) -> None:
        self.tree.clear()
        client = self.host.lan_client
        if err:
            self.lbl_path.setText(f"{path_show} · {err}")
            self._append_log(f"列表失败：{err}")
            return
        entries = list(entries or [])
        files = [e for e in entries if not (e.get("is_dir") or e.get("type") == "dir" or e.get("dir"))]
        total = sum(int(e.get("size") or 0) for e in files)
        root_name = getattr(client, "remote_root_name", "") or "共享"
        self.lbl_path.setText(
            f"已连接 · {len(entries)} 项 · 共 {_fmt_size(total)}  ·  {root_name}{path_show}"
        )
        self._append_log(f"列表成功：{len(entries)} 项（文件 {len(files)}，{_fmt_size(total)}）")

        def _key(e: dict):
            return (0 if e.get("is_dir") or e.get("type") == "dir" else 1, str(e.get("name") or "").lower())

        for e in sorted(entries, key=_key):
            name = str(e.get("name") or e.get("path") or "?")
            is_dir = bool(e.get("is_dir") or e.get("type") == "dir" or e.get("dir"))
            size = e.get("size")
            rel = str(e.get("path") or e.get("rel") or "")
            if not rel:
                rel = f"{self._remote_path.rstrip('/')}/{name}".lstrip("/") if self._remote_path else name
            item = QTreeWidgetItem(
                [
                    ("📁 " if is_dir else "📄 ") + name,
                    "文件夹" if is_dir else "文件",
                    "" if is_dir else _fmt_size(size),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, {"path": rel, "is_dir": is_dir, "name": name})
            self.tree.addTopLevelItem(item)

    def _on_item_dbl(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("is_dir"):
            self._remote_path = str(data.get("path") or "")
            self._refresh_list()
        else:
            self._start_download(
                [{"path": str(data.get("path") or ""), "name": str(data.get("name") or "file"), "is_dir": False}]
            )

    def _go_up(self) -> None:
        if not self._remote_path:
            return
        parts = self._remote_path.replace("\\", "/").strip("/").split("/")
        self._remote_path = "/".join(parts[:-1])
        self._refresh_list()

    def _download_selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self._set_status("请先选择要下载的文件或文件夹")
            self._append_log("下载取消：未选择任何项")
            return
        jobs = []
        for item in items:
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            path = str(data.get("path") or "")
            name = str(data.get("name") or "file")
            if not path:
                continue
            jobs.append({"path": path, "name": name, "is_dir": bool(data.get("is_dir"))})
        if not jobs:
            self._set_status("选中项无效")
            self._append_log("下载取消：选中项没有有效路径")
            return
        self._start_download(jobs)

    def _update_download_dir_label(self) -> None:
        path = getattr(self, "_download_dir", "") or str(Path.home() / "Downloads")
        self._download_dir = path
        if hasattr(self, "lbl_download_dir"):
            self.lbl_download_dir.setText(path)

    def _change_download_dir(self) -> None:
        dest = self._pick_download_dir(force=True)
        if dest:
            self._append_log(f"保存目录已改为：{dest}")
            self._set_status(f"保存目录：{dest}")

    def _open_download_dir(self) -> None:
        path = Path(getattr(self, "_download_dir", "") or Path.home() / "Downloads")
        try:
            path.mkdir(parents=True, exist_ok=True)
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            self._append_log(f"已打开目录：{path}")
        except Exception as e:
            self._append_log(f"打开目录失败：{e}")
            self._set_status(f"打开目录失败：{e}")

    def _pick_download_dir(self, *, force: bool = False) -> str | None:
        current = getattr(self, "_download_dir", None) or str(Path.home() / "Downloads")
        if not force and current and Path(current).is_dir():
            return current
        dest = QFileDialog.getExistingDirectory(
            self._dialog_parent(),
            "保存到文件夹…",
            current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not dest:
            return None
        self._download_dir = dest
        self._update_download_dir_label()
        self._persist_fields()
        return dest

    def _copy_share_link(self) -> None:
        ips = local_ipv4_addresses() or []
        # Prefer real LAN IPs; 127.0.0.1 often fails to accept on some Windows setups
        ip = next((x for x in ips if not x.startswith("127.")), None) or (ips[0] if ips else "127.0.0.1")
        port = int(self.spin_port.value())
        link = f"http://{ip}:{port}"
        try:
            QGuiApplication.clipboard().setText(link)
            tip = f"已复制分享地址：{link}（对方填 IP {ip}，端口 {port}，密码见上方）"
            self._set_status(tip)
            self._append_log(tip)
            if not getattr(self.host.lan_server, "running", False):
                self._append_log("提示：主机尚未启动，对方现在还连不上")
        except Exception as e:
            self._append_log(f"复制失败：{e}")

    def _start_download(self, jobs: list[dict]) -> None:
        if self._downloading:
            self._set_status("已有下载任务进行中，请稍候")
            self._append_log("下载跳过：上一个任务尚未结束")
            return
        if not getattr(self.host.lan_client, "connected", False):
            self._set_status("尚未连接远端")
            self._append_log("下载失败：尚未连接")
            return
        dest_dir = self._pick_download_dir(force=False)
        if not dest_dir:
            self._append_log("下载取消：未选择保存目录（点「更改目录」先选好）")
            self._set_status("请先选择保存目录")
            return
        try:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._append_log(f"无法创建保存目录：{e}")
            self._set_status(f"保存目录无效：{e}")
            return

        self._downloading = True
        self.btn_download.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        names = "、".join(j["name"] for j in jobs[:3])
        if len(jobs) > 3:
            names += f" 等 {len(jobs)} 项"
        self._append_log(f"开始下载 {names} → {dest_dir}")
        self._set_status(f"下载中… 0/{len(jobs)}")
        client = self.host.lan_client

        def work() -> None:
            ok = fail = 0
            total_jobs = len(jobs)
            for i, job in enumerate(jobs):
                rel = job["path"]
                name = job["name"]
                is_dir = job["is_dir"]
                self._bridge.progress.emit(i, total_jobs, name)
                self._bridge.log.emit(f"下载 ({i + 1}/{total_jobs})：{rel}")
                try:
                    if is_dir:

                        def folder_prog(local_rel: str, cur: int, tot: int) -> None:
                            self._bridge.progress.emit(cur, max(1, tot), local_rel)
                            self._bridge.log.emit(f"  · {local_rel} ({cur}/{tot})")

                        msg = client.download_folder(
                            rel, Path(dest_dir) / name, progress=folder_prog
                        )
                    else:

                        def file_prog(written: int, total: int) -> None:
                            # map byte progress into current job slot
                            pct = int(written * 100 / max(1, total)) if total else 0
                            self._bridge.progress.emit(i * 100 + pct, total_jobs * 100, name)

                        msg = client.download(
                            rel, Path(dest_dir) / name, progress=file_prog
                        )
                except Exception as e:
                    msg = f"下载失败：{e}"
                self._bridge.log.emit(msg)
                if str(msg).startswith("已下载") or str(msg).startswith("文件夹完成"):
                    ok += 1
                else:
                    fail += 1
            summary = f"下载结束：成功 {ok}，失败 {fail} → {dest_dir}"
            self._bridge.download_done.emit(summary)

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, cur: int, total: int, name: str) -> None:
        total = max(1, int(total))
        cur = max(0, int(cur))
        # Support both job-index and percent-scaled progress
        if total >= 100 and cur > total:
            cur = total
        pct = int(cur * 100 / total)
        self.progress.setValue(min(100, pct))
        self.progress.setFormat(f"{name} · {pct}%")
        self._set_status(f"下载中：{name}（{pct}%）")

    def _on_download_done(self, summary: str) -> None:
        self._downloading = False
        self.btn_download.setEnabled(True)
        self.progress.setValue(100)
        self.progress.setFormat("完成")
        self._set_status(summary)
        self._append_log(summary)
        try:
            self.host.announce(summary)
        except Exception:
            pass
        QMessageBox.information(self._dialog_parent(), "下载完成", summary)

    # ---- window drag (floating only) ----
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self.embedded:
            super().mousePressEvent(e)
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self.embedded:
            super().mouseMoveEvent(e)
            return
        if self.dragging and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag_pos)
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        self.dragging = False
        super().mouseReleaseEvent(e)
