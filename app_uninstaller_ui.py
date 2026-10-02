"""Embedded / floating UI for listing and uninstalling Windows programs."""

from __future__ import annotations

import sys
import threading
from typing import Callable

from PyQt6.QtCore import Qt, QObject, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app_uninstaller import (
    CleanupReport,
    InstalledApp,
    cleanup_after_uninstall,
    format_size_kb,
    icon_for_app,
    list_installed_apps,
    run_uninstall,
    supported,
)


class _Bridge(QObject):
    listed = pyqtSignal(object)
    log_line = pyqtSignal(str)
    finished = pyqtSignal(str, object)  # status, CleanupReport|None


class AppUninstallerPanel(QWidget):
    """List installed apps, run uninstallers, then clean leftover registry keys."""

    DARK_QSS = """
        QWidget#uninstallRoot { background: transparent; }
        QLabel { color: #e2e8f0; font-weight: 600; }
        QLabel#muted { color: #94a3b8; font-size: 12px; font-weight: 500; }
        QLineEdit {
            background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
            border-radius: 8px; padding: 8px 10px;
        }
        QListWidget {
            background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
            border-radius: 8px; padding: 4px;
        }
        QListWidget::item { padding: 6px 10px; border-radius: 6px; min-height: 44px; }
        QListWidget::item:selected { background: #312e81; color: #e0e7ff; }
        QListWidget::item:hover { background: #1e293b; }
        QPlainTextEdit {
            background: #0f172a; color: #cbd5e1; border: 1px solid #334155;
            border-radius: 8px; font-family: Consolas, "Courier New", monospace;
            font-size: 12px;
        }
        QCheckBox { color: #e2e8f0; spacing: 8px; }
        QPushButton {
            background: #6366f1; color: white; border: none;
            border-radius: 8px; padding: 8px 14px; font-weight: 700;
        }
        QPushButton:hover { background: #818cf8; }
        QPushButton:disabled { background: #334155; color: #64748b; }
        QPushButton#soft {
            background: #1e293b; color: #e2e8f0; border: 1px solid #475569;
        }
        QPushButton#soft:hover { background: #334155; border-color: #64748b; }
        QPushButton#danger {
            background: #b91c1c; color: white;
        }
        QPushButton#danger:hover { background: #dc2626; }
        QFrame#panel {
            background: #111827; border: 1px solid rgba(99,102,241,0.28);
            border-radius: 12px;
        }
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        embedded: bool = True,
        on_announce: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.embedded = embedded
        self.on_announce = on_announce
        self._apps: list[InstalledApp] = []
        self._busy = False
        self._bridge = _Bridge(self)
        self._bridge.listed.connect(self._on_listed)
        self._bridge.log_line.connect(self._append_log)
        self._bridge.finished.connect(self._on_finished)

        self.setObjectName("uninstallRoot")
        self.setStyleSheet(self.DARK_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(0 if embedded else 12, 0 if embedded else 12, 0 if embedded else 12, 0 if embedded else 12)
        root.setSpacing(10)

        tip = QLabel(
            "列出本机「应用和功能」同类项。卸载会调用官方卸载程序；"
            "完成后可清理残留的 Uninstall 注册表项（可选清理空的 Software 产品键）。"
            "系统组件默认隐藏。需要管理员权限的程序请以管理员运行本工具。"
        )
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        if not supported():
            root.addWidget(QLabel("当前系统不是 Windows，无法使用软件卸载与注册表清理。"))
            return

        filter_row = QHBoxLayout()
        self.txt_filter = QLineEdit()
        self.txt_filter.setPlaceholderText("搜索名称或发布者…")
        self.txt_filter.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.txt_filter, 1)
        self.btn_refresh = QPushButton("刷新", objectName="soft")
        self.btn_refresh.clicked.connect(self.refresh)
        filter_row.addWidget(self.btn_refresh)
        root.addLayout(filter_row)

        opts = QHBoxLayout()
        self.chk_quiet = QCheckBox("优先静默卸载（若提供 QuietUninstallString）")
        self.chk_reg = QCheckBox("卸载后清理残留注册表")
        self.chk_reg.setChecked(True)
        self.chk_soft = QCheckBox("同时清理空的 Software\\发布者\\产品 键")
        self.chk_soft.setChecked(True)
        opts.addWidget(self.chk_quiet)
        opts.addWidget(self.chk_reg)
        opts.addWidget(self.chk_soft)
        opts.addStretch(1)
        root.addLayout(opts)

        panel = QFrame(objectName="panel")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(8)
        self.lbl_count = QLabel("正在加载已安装程序…")
        self.lbl_count.setObjectName("muted")
        pl.addWidget(self.lbl_count)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setAlternatingRowColors(False)
        self.list.setIconSize(QSize(32, 32))
        self.list.setSpacing(2)
        pl.addWidget(self.list, 1)
        root.addWidget(panel, 3)

        act = QHBoxLayout()
        self.btn_uninstall = QPushButton("卸载选中")
        self.btn_uninstall.setObjectName("danger")
        self.btn_uninstall.setMinimumHeight(36)
        self.btn_uninstall.clicked.connect(self._uninstall_selected)
        self.btn_clean_only = QPushButton("仅清理注册表残留", objectName="soft")
        self.btn_clean_only.setMinimumHeight(36)
        self.btn_clean_only.setToolTip("不运行卸载程序，只删除仍残留的 Uninstall / 空 Software 键")
        self.btn_clean_only.clicked.connect(self._clean_selected_only)
        act.addWidget(self.btn_uninstall)
        act.addWidget(self.btn_clean_only)
        act.addStretch(1)
        root.addLayout(act)

        self.lbl_status = QLabel("选择一个程序后点「卸载选中」。")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("操作日志…")
        self.log.setMaximumHeight(140)
        root.addWidget(self.log, 1)

        QTimer.singleShot(50, self.refresh)

    def _announce(self, msg: str) -> None:
        if self.on_announce:
            try:
                self.on_announce(msg)
            except Exception:
                pass

    def _append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.setEnabled(not busy)
        self.btn_uninstall.setEnabled(not busy)
        self.btn_clean_only.setEnabled(not busy)
        self.txt_filter.setEnabled(not busy)

    def refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.lbl_count.setText("正在扫描注册表…")
        self.lbl_status.setText("扫描中…")

        def work() -> None:
            try:
                apps = list_installed_apps(include_system=False)
                self._bridge.listed.emit(apps)
            except Exception as exc:
                self._bridge.listed.emit(exc)

        threading.Thread(target=work, daemon=True).start()

    def _on_listed(self, payload) -> None:
        self._set_busy(False)
        if isinstance(payload, Exception):
            self.lbl_count.setText("扫描失败")
            self.lbl_status.setText(f"扫描失败：{payload}")
            self._append_log(f"扫描失败：{payload}")
            return
        self._apps = list(payload or [])
        self._apply_filter()
        self.lbl_status.setText(f"共 {len(self._apps)} 个可卸载程序。")
        self._append_log(f"已刷新，共 {len(self._apps)} 项。")

    def _apply_filter(self) -> None:
        q = (self.txt_filter.text() or "").strip().lower()
        self.list.clear()
        shown = 0
        for app in self._apps:
            blob = f"{app.display_name} {app.publisher} {app.version}".lower()
            if q and q not in blob:
                continue
            size = format_size_kb(app.estimated_size_kb)
            pub = app.publisher or "未知发布者"
            ver = app.version or "—"
            text = f"{app.display_name}\n{pub}  ·  v{ver}  ·  {size}  ·  {app.source_label}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, app)
            try:
                icon = icon_for_app(app)
                if isinstance(icon, QIcon) and not icon.isNull():
                    item.setIcon(icon)
            except Exception:
                pass
            self.list.addItem(item)
            shown += 1
        self.lbl_count.setText(f"显示 {shown} / {len(self._apps)}")

    def _selected_app(self) -> InstalledApp | None:
        item = self.list.currentItem()
        if not item:
            return None
        app = item.data(Qt.ItemDataRole.UserRole)
        return app if isinstance(app, InstalledApp) else None

    def _confirm(self, title: str, body: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(body)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _uninstall_selected(self) -> None:
        app = self._selected_app()
        if not app:
            self.lbl_status.setText("请先选择一个程序。")
            return
        if self._busy:
            return
        clean_reg = bool(self.chk_reg.isChecked())
        clean_soft = bool(self.chk_soft.isChecked())
        quiet = bool(self.chk_quiet.isChecked())
        extra = "并在结束后清理残留注册表。" if clean_reg else "（不清理注册表）"
        if not self._confirm(
            "确认卸载",
            f"即将卸载：\n\n{app.display_name}\n发布者：{app.publisher or '—'}\n版本：{app.version or '—'}\n\n"
            f"会启动官方卸载程序{extra}\n是否继续？",
        ):
            return

        self._set_busy(True)
        self.lbl_status.setText(f"正在卸载「{app.display_name}」…")
        self._announce(f"开始卸载 {app.display_name}")

        def work() -> None:
            report: CleanupReport | None = None
            try:
                def log(msg: str) -> None:
                    self._bridge.log_line.emit(msg)

                log(f"开始卸载：{app.display_name}")
                code = run_uninstall(app, prefer_quiet=quiet, log=log)
                log(f"卸载进程退出码：{code}")
                if clean_reg:
                    log("开始清理残留注册表…")
                    report = cleanup_after_uninstall(
                        app, clean_software_keys=clean_soft, log=log
                    )
                status = f"完成：{app.display_name}（退出码 {code}）"
                self._bridge.finished.emit(status, report)
            except Exception as exc:
                self._bridge.finished.emit(f"失败：{exc}", None)

        threading.Thread(target=work, daemon=True).start()

    def _clean_selected_only(self) -> None:
        app = self._selected_app()
        if not app:
            self.lbl_status.setText("请先选择一个程序。")
            return
        if self._busy:
            return
        if not self._confirm(
            "仅清理注册表",
            f"不会运行卸载程序，只尝试删除与「{app.display_name}」匹配的残留 Uninstall 项"
            f"{' 及空的 Software 产品键' if self.chk_soft.isChecked() else ''}。\n\n是否继续？",
        ):
            return

        self._set_busy(True)
        self.lbl_status.setText(f"正在清理「{app.display_name}」注册表残留…")

        def work() -> None:
            try:
                def log(msg: str) -> None:
                    self._bridge.log_line.emit(msg)

                report = cleanup_after_uninstall(
                    app,
                    wait_sec=0.2,
                    clean_software_keys=bool(self.chk_soft.isChecked()),
                    log=log,
                )
                self._bridge.finished.emit(f"注册表清理结束：{app.display_name}", report)
            except Exception as exc:
                self._bridge.finished.emit(f"清理失败：{exc}", None)

        threading.Thread(target=work, daemon=True).start()

    def _on_finished(self, status: str, report) -> None:
        self._set_busy(False)
        self.lbl_status.setText(status)
        self._announce(status)
        if isinstance(report, CleanupReport):
            self._append_log(
                f"清理结果：删除 {len(report.removed_keys)} 项，"
                f"跳过 {len(report.skipped)}，错误 {len(report.errors)}"
            )
        # Refresh list so removed apps disappear
        QTimer.singleShot(400, self.refresh)
