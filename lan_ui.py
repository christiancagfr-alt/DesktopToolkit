"""LAN share board: compact settings + large remote file list."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QPoint, QSize
from PyQt6.QtGui import QMouseEvent, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QSplitter,
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


class FloatingLanBoard(QWidget):
    def __init__(self, host, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self.host = host
        self.embedded = embedded
        self.dragging = False
        self.drag_pos = QPoint()
        self._remote_path = ""
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
        self._refresh_status()

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
            QLineEdit, QSpinBox {
                background: #020617; color: #f8fafc; border: 1px solid #334155;
                border-radius: 8px; padding: 5px 8px; min-height: 22px; font-size: 12px;
            }
            QPushButton {
                background: #6366f1; color: white; border: none; border-radius: 8px;
                padding: 6px 10px; font-weight: 700; font-size: 12px;
            }
            QPushButton#soft { background: #1e293b; border: 1px solid #334155; }
            QPushButton#danger { background: #dc2626; }
            QTreeWidget, QListWidget {
                background: #020617; color: #e2e8f0; border: 1px solid #334155;
                border-radius: 10px; font-size: 12px; outline: none;
            }
            QTreeWidget::item, QListWidget::item {
                padding: 4px 6px; border-radius: 4px;
            }
            QTreeWidget::item:selected, QListWidget::item:selected {
                background: #4338ca; color: white;
            }
            QHeaderView::section {
                background: #1e293b; color: #a5b4fc; border: none;
                padding: 6px; font-weight: 700;
            }
            QSplitter::handle { background: #1e293b; height: 3px; }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame(objectName="box")
        # When embedded in main window, no outer chrome / no × (main window has its own close)
        if self.embedded:
            box.setStyleSheet(
                "QFrame#box { background: transparent; border: none; border-radius: 0; }"
            )
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4 if self.embedded else 12, 4 if self.embedded else 10, 4 if self.embedded else 12, 8)
        lay.setSpacing(8)

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

        # ---- Compact settings (host + client) ----
        settings = QFrame(objectName="settingsBox")
        s = QVBoxLayout(settings)
        s.setContentsMargins(10, 8, 10, 8)
        s.setSpacing(6)

        self.lbl_ips = QLabel()
        self.lbl_ips.setObjectName("muted")
        self.lbl_ips.setWordWrap(True)
        s.addWidget(self.lbl_ips)

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
        row2.addWidget(self.btn_start)
        row2.addWidget(self.btn_stop)
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
        # default same as host pwd for convenience when testing self
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

        # Settings take limited height; file area gets the rest
        settings.setMaximumHeight(210)
        lay.addWidget(settings)

        # ---- File browser (main area) ----
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
        self.btn_download.clicked.connect(self._download_selected)
        file_head.addWidget(self.btn_up)
        file_head.addWidget(self.btn_refresh)
        file_head.addWidget(self.btn_download)
        lay.addLayout(file_head)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "类型", "大小"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._on_item_dbl)
        lay.addWidget(self.tree, 1)

        root.addWidget(box)

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

    def _pick(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "分享目录", self.txt_root.text())
        if p:
            self.txt_root.setText(p)

    def _start_host(self) -> None:
        msg = self.host.lan_server.start(
            self.txt_root.text().strip(),
            self.txt_pwd.text().strip() or "tool1234",
            int(self.spin_port.value()),
        )
        self.lbl_status.setText(msg)
        self._refresh_status()

    def _stop_host(self) -> None:
        msg = self.host.lan_server.stop()
        self.lbl_status.setText(msg or "已停止")
        self._refresh_status()

    def _connect(self) -> None:
        host = self.txt_host.text().strip()
        if not host:
            self.lbl_status.setText("请填写主机 IP")
            return
        pwd = self.txt_client_pwd.text().strip()
        if not pwd:
            self.lbl_status.setText("请填写访问密码")
            return
        msg = self.host.lan_client.connect(host, int(self.spin_port.value()), pwd)
        self.lbl_status.setText(msg)
        self._refresh_status()
        if self.host.lan_client.connected:
            self._remote_path = ""
            self._refresh_list()

    def _disconnect(self) -> None:
        try:
            self.host.lan_client.disconnect()
            msg = "已断开"
        except Exception as e:
            msg = str(e)
        self.lbl_status.setText(msg)
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
        err, entries = client.list_dir(self._remote_path)
        path_show = self._remote_path or "/"
        if err:
            self.lbl_path.setText(f"{path_show} · {err}")
            return
        self.lbl_path.setText(f"{client.remote_root_name}{path_show if path_show != '/' else '/'}")
        # folders first
        def _key(e: dict):
            return (0 if e.get("is_dir") or e.get("type") == "dir" else 1, str(e.get("name") or "").lower())

        for e in sorted(entries or [], key=_key):
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
            self._download_one(str(data.get("path") or ""), str(data.get("name") or "file"))

    def _go_up(self) -> None:
        if not self._remote_path:
            return
        parts = self._remote_path.replace("\\", "/").strip("/").split("/")
        self._remote_path = "/".join(parts[:-1])
        self._refresh_list()

    def _download_selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self.lbl_status.setText("请先选择要下载的文件")
            return
        for item in items:
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("is_dir"):
                continue
            self._download_one(str(data.get("path") or ""), str(data.get("name") or "file"))

    def _download_one(self, rel: str, name: str) -> None:
        if not rel:
            return
        dest_dir = QFileDialog.getExistingDirectory(self, "保存到…", str(Path.home() / "Downloads"))
        if not dest_dir:
            return
        dest = Path(dest_dir) / name
        msg = self.host.lan_client.download(rel, dest)
        self.lbl_status.setText(msg)
        try:
            self.host.announce(msg)
        except Exception:
            pass

    def mousePressEvent(self, e: QMouseEvent) -> None:
        if self.embedded:
            super().mousePressEvent(e)
            return
        if e.button() == Qt.MouseButton.LeftButton:
            # Floating mode only: drag from empty chrome
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
