"""UI panel for secure file shredding."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from file_shredder import (
    DEFAULT_PASSES,
    MAX_PASSES,
    MIN_PASSES,
    ShredReport,
    clamp_passes,
    collect_targets,
    estimate_bytes,
    register_shell_context,
    shell_context_registered,
    shred_many,
    unregister_shell_context,
)


class _Bridge(QObject):
    progress = pyqtSignal(str, float)
    log = pyqtSignal(str)
    finished = pyqtSignal(object)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


class FileShredderPanel(QWidget):
    """Embedded / floating shredder: add files, set passes, confirm, shred."""

    def __init__(
        self,
        parent=None,
        *,
        embedded: bool = False,
        state: dict | None = None,
        on_save_state=None,
        on_announce=None,
    ):
        super().__init__(parent)
        self.embedded = embedded
        self.state = state if isinstance(state, dict) else {}
        self.on_save_state = on_save_state
        self.on_announce = on_announce
        self._busy = False
        self._cancel = False
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.log.connect(self._append_log)
        self._bridge.finished.connect(self._on_finished)

        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        else:
            self.setWindowFlags(Qt.WindowType.Window)
            self.setWindowTitle("粉碎文件")
            self.resize(640, 560)

        self.setAcceptDrops(True)
        self._build()
        self._apply_style()
        self._load_prefs()

    def _cfg(self) -> dict:
        return self.state.setdefault("shredder", {})

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        m = 4 if self.embedded else 14
        lay.setContentsMargins(m, m, m, m)
        lay.setSpacing(10)

        if not self.embedded:
            title = QLabel("粉碎文件")
            title.setObjectName("title")
            lay.addWidget(title)

        tip = QLabel(
            "安全覆盖后直接删除（不进回收站）。可把文件拖到本面板或悬浮机器人上。"
            "默认 3 次覆盖（零 → 满 → 随机），可在下方调整。"
        )
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addWidget(QLabel("粉碎次数"))
        self.spin_passes = QSpinBox()
        self.spin_passes.setRange(MIN_PASSES, MAX_PASSES)
        self.spin_passes.setValue(DEFAULT_PASSES)
        self.spin_passes.setToolTip("覆盖写入次数（1–35）。次数越多越慢、越难恢复。")
        self.spin_passes.valueChanged.connect(self._persist)
        row.addWidget(self.spin_passes)
        self.chk_rename = QCheckBox("粉碎前随机改名")
        self.chk_rename.setChecked(True)
        self.chk_rename.setToolTip("覆盖后将文件名改为随机短名再删除，降低按名恢复可能。")
        self.chk_rename.toggled.connect(self._persist)
        row.addWidget(self.chk_rename)
        row.addStretch(1)
        lay.addLayout(row)

        self.list = QListWidget()
        self.list.setMinimumHeight(160)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setToolTip("可拖拽文件/文件夹到此处")
        lay.addWidget(self.list, 1)

        btns = QHBoxLayout()
        b_add = QPushButton("添加文件…", objectName="soft")
        b_add.clicked.connect(self._add_files)
        btns.addWidget(b_add)
        b_dir = QPushButton("添加文件夹…", objectName="soft")
        b_dir.clicked.connect(self._add_folder)
        btns.addWidget(b_dir)
        b_rm = QPushButton("移除选中", objectName="soft")
        b_rm.clicked.connect(self._remove_selected)
        btns.addWidget(b_rm)
        b_clear = QPushButton("清空列表", objectName="soft")
        b_clear.clicked.connect(self.list.clear)
        btns.addWidget(b_clear)
        btns.addStretch(1)
        lay.addLayout(btns)

        self.lbl_stats = QLabel("未添加文件")
        self.lbl_stats.setObjectName("muted")
        lay.addWidget(self.lbl_stats)

        act = QHBoxLayout()
        self.btn_shred = QPushButton("开始粉碎")
        self.btn_shred.setObjectName("danger")
        self.btn_shred.setMinimumHeight(36)
        self.btn_shred.clicked.connect(self._start)
        act.addWidget(self.btn_shred)
        self.btn_cancel = QPushButton("取消", objectName="soft")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._request_cancel)
        act.addWidget(self.btn_cancel)
        lay.addLayout(act)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        lay.addWidget(self.progress)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("muted")
        lay.addWidget(self.lbl_status)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        lay.addWidget(self.log)

        if sys.platform == "win32":
            shell_row = QHBoxLayout()
            self.chk_shell = QCheckBox("在资源管理器右键菜单中显示「用 Desktop Toolkit 粉碎」")
            self.chk_shell.toggled.connect(self._on_shell_toggled)
            shell_row.addWidget(self.chk_shell)
            shell_row.addStretch(1)
            lay.addLayout(shell_row)
            hint = QLabel("勾选后可在文件/文件夹上右键粉碎；卸载或取消勾选会移除菜单项。")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            lay.addWidget(hint)

        self.list.model().rowsInserted.connect(lambda *_: self._refresh_stats())
        self.list.model().rowsRemoved.connect(lambda *_: self._refresh_stats())

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget { color: #e2e8f0; font-size: 12px; }
            QLabel#title { color: #f87171; font-size: 16px; font-weight: 800; }
            QLabel#muted { color: #94a3b8; font-size: 11px; }
            QListWidget, QTextEdit {
                background: rgba(15,23,42,0.9); border: 1px solid #334155;
                border-radius: 8px; padding: 4px;
            }
            QSpinBox {
                background: #0f172a; color: #f8fafc; border: 1px solid #334155;
                border-radius: 6px; padding: 4px 8px; min-width: 64px;
            }
            QPushButton {
                background: #6366f1; color: white; border: none; border-radius: 8px;
                padding: 8px 14px; font-weight: 700;
            }
            QPushButton#soft {
                background: #1e293b; border: 1px solid #334155; color: #e2e8f0;
            }
            QPushButton#danger { background: #dc2626; }
            QPushButton:disabled { background: #334155; color: #94a3b8; }
            QProgressBar {
                border: 1px solid #334155; border-radius: 6px; text-align: center;
                background: #0f172a; height: 18px;
            }
            QProgressBar::chunk { background: #dc2626; border-radius: 5px; }
            QCheckBox { spacing: 8px; }
            """
        )

    def _load_prefs(self) -> None:
        cfg = self._cfg()
        self.spin_passes.setValue(clamp_passes(cfg.get("passes", DEFAULT_PASSES)))
        self.chk_rename.setChecked(bool(cfg.get("rename", True)))
        if sys.platform == "win32" and hasattr(self, "chk_shell"):
            # Default off until user opts in (or installer registry already present)
            want = bool(cfg["shell_menu"]) if "shell_menu" in cfg else shell_context_registered()
            self.chk_shell.blockSignals(True)
            self.chk_shell.setChecked(want)
            self.chk_shell.blockSignals(False)
            if want and not shell_context_registered():
                try:
                    register_shell_context()
                except Exception:
                    pass

    def _persist(self) -> None:
        cfg = self._cfg()
        cfg["passes"] = int(self.spin_passes.value())
        cfg["rename"] = bool(self.chk_rename.isChecked())
        if sys.platform == "win32" and hasattr(self, "chk_shell"):
            cfg["shell_menu"] = bool(self.chk_shell.isChecked())
        if callable(self.on_save_state):
            try:
                self.on_save_state()
            except Exception:
                pass

    def _on_shell_toggled(self, on: bool) -> None:
        try:
            if on:
                register_shell_context()
                self._append_log("已添加资源管理器右键菜单")
            else:
                unregister_shell_context()
                self._append_log("已移除资源管理器右键菜单")
        except Exception as exc:
            QMessageBox.warning(self, "右键菜单", f"设置失败：{exc}")
            self.chk_shell.blockSignals(True)
            self.chk_shell.setChecked(shell_context_registered())
            self.chk_shell.blockSignals(False)
        self._persist()

    def _paths(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            out.append(self.list.item(i).data(Qt.ItemDataRole.UserRole))
        return out

    def add_paths(self, paths: list[str | Path], *, select_page_hint: bool = False) -> int:
        """Add paths to the queue. Returns number newly added."""
        existing = set(self._paths())
        added = 0
        for raw in paths:
            p = str(Path(raw))
            if not p or p in existing:
                continue
            if not Path(p).exists():
                continue
            item = QListWidgetItem(p)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.list.addItem(item)
            existing.add(p)
            added += 1
        self._refresh_stats()
        return added

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要粉碎的文件")
        if files:
            self.add_paths(files)

    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要粉碎的文件夹")
        if d:
            self.add_paths([d])

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        paths = self._paths()
        if not paths:
            self.lbl_stats.setText("未添加文件")
            return
        targets, errs = collect_targets(paths)
        files = [p for p in targets if p.is_file() or p.is_symlink()]
        dirs = [p for p in targets if p.is_dir() and not p.is_symlink()]
        nbytes = estimate_bytes(targets)
        extra = f"；跳过 {len(errs)} 项" if errs else ""
        self.lbl_stats.setText(
            f"队列 {len(paths)} 项 → 约 {len(files)} 个文件 / {len(dirs)} 个目录，"
            f"合计 {_fmt_size(nbytes)}{extra}"
        )

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        paths = []
        for url in e.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.add_paths(paths)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_shred.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.spin_passes.setEnabled(not busy)
        self.chk_rename.setEnabled(not busy)
        self.list.setEnabled(not busy)

    def _request_cancel(self) -> None:
        self._cancel = True
        self.lbl_status.setText("正在取消…")

    def _append_log(self, msg: str) -> None:
        self.log.append(msg)

    def _on_progress(self, msg: str, frac: float) -> None:
        self.lbl_status.setText(msg)
        self.progress.setValue(int(max(0.0, min(1.0, frac)) * 1000))

    def _announce(self, msg: str) -> None:
        if callable(self.on_announce):
            try:
                self.on_announce(msg)
            except Exception:
                pass

    def _start(self) -> None:
        if self._busy:
            return
        paths = self._paths()
        if not paths:
            QMessageBox.information(self, "粉碎文件", "请先添加要粉碎的文件或文件夹。")
            return
        passes = clamp_passes(self.spin_passes.value())
        rename = self.chk_rename.isChecked()
        targets, errs = collect_targets(paths)
        files = [p for p in targets if p.is_file() or p.is_symlink()]
        nbytes = estimate_bytes(targets)
        warn = (
            f"即将永久粉碎（不进回收站）：\n"
            f"· {len(files)} 个文件，约 {_fmt_size(nbytes)}\n"
            f"· 覆盖次数：{passes}\n"
            f"· 随机改名：{'是' if rename else '否'}\n\n"
            f"此操作不可恢复。确定继续？"
        )
        if errs:
            warn += f"\n\n另有 {len(errs)} 项将被跳过。"
        ret = QMessageBox.warning(
            self,
            "确认粉碎",
            warn,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self._persist()
        self._cancel = False
        self._set_busy(True)
        self.progress.setValue(0)
        self.log.clear()
        self._append_log(f"开始粉碎，次数={passes}")

        def worker() -> None:
            report = shred_many(
                paths,
                passes=passes,
                rename=rename,
                progress=lambda m, f: self._bridge.progress.emit(m, f),
                log=lambda m: self._bridge.log.emit(m),
                cancel=lambda: self._cancel,
            )
            self._bridge.finished.emit(report)

        threading.Thread(target=worker, daemon=True).start()

    def _on_finished(self, report: object) -> None:
        self._set_busy(False)
        if not isinstance(report, ShredReport):
            return
        # Remove successfully shredded top-level queue items that no longer exist
        keep = []
        for p in self._paths():
            if Path(p).exists():
                keep.append(p)
        self.list.clear()
        self.add_paths(keep)
        msg = report.summary()
        self.lbl_status.setText(msg)
        self._announce(msg)
        if report.fail_count and not report.cancelled:
            QMessageBox.warning(self, "粉碎结束", msg)
        else:
            QMessageBox.information(self, "粉碎结束", msg)

    def offer_shred(self, paths: list[str | Path]) -> None:
        """Hub-panel entry: queue paths and run the in-page confirm flow."""
        n = self.add_paths(paths)
        if n == 0 and not self._paths():
            QMessageBox.information(self, "粉碎文件", "没有可粉碎的有效路径。")
            return
        self._start()


class QuickShredDialog(QDialog):
    """Lightweight confirm + progress window — does not open the main hub."""

    def __init__(
        self,
        paths: list[str],
        *,
        passes: int,
        rename: bool,
        on_announce=None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("粉碎文件")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(420, 180)
        self._paths = list(paths)
        self._passes = clamp_passes(passes)
        self._rename = bool(rename)
        self._on_announce = on_announce
        self._cancel = False
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.finished.connect(self._on_finished)

        lay = QVBoxLayout(self)
        self.lbl = QLabel("准备粉碎…")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._request_cancel)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)

    def _request_cancel(self) -> None:
        self._cancel = True
        self.lbl.setText("正在取消…")
        self.btn_cancel.setEnabled(False)

    def _on_progress(self, msg: str, frac: float) -> None:
        self.lbl.setText(msg)
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))

    def start(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

        def worker() -> None:
            report = shred_many(
                self._paths,
                passes=self._passes,
                rename=self._rename,
                progress=lambda m, f: self._bridge.progress.emit(m, f),
                cancel=lambda: self._cancel,
            )
            self._bridge.finished.emit(report)

        threading.Thread(target=worker, daemon=True).start()

    def _on_finished(self, report: object) -> None:
        self.btn_cancel.setEnabled(False)
        if not isinstance(report, ShredReport):
            self.close()
            return
        msg = report.summary()
        if callable(self._on_announce):
            try:
                self._on_announce(msg)
            except Exception:
                pass
        self.lbl.setText(msg)
        self.bar.setValue(1000)
        if report.fail_count and not report.cancelled:
            QMessageBox.warning(self, "粉碎结束", msg)
        else:
            QMessageBox.information(self, "粉碎结束", msg)
        self.accept()


def quick_shred(
    paths: list[str | Path],
    *,
    state: dict | None = None,
    on_announce=None,
    parent=None,
) -> QuickShredDialog | None:
    """Confirm and shred without opening the main hub UI.

    Returns the progress dialog (keep a reference) or None if cancelled / empty.
    """
    cleaned = [str(p) for p in (paths or []) if str(p).strip()]
    cleaned = [p for p in cleaned if Path(p).exists()]
    if not cleaned:
        QMessageBox.information(parent, "粉碎文件", "没有可粉碎的有效路径。")
        return None

    cfg = (state or {}).setdefault("shredder", {}) if isinstance(state, dict) else {}
    passes = clamp_passes(cfg.get("passes", DEFAULT_PASSES) if isinstance(cfg, dict) else DEFAULT_PASSES)
    rename = bool(cfg.get("rename", True)) if isinstance(cfg, dict) else True

    targets, errs = collect_targets(cleaned)
    files = [p for p in targets if p.is_file() or p.is_symlink()]
    nbytes = estimate_bytes(targets)
    names = [Path(p).name for p in cleaned[:5]]
    name_line = "、".join(names)
    if len(cleaned) > 5:
        name_line += f" 等 {len(cleaned)} 项"

    warn = (
        f"确定粉碎以下内容？\n"
        f"· {name_line}\n"
        f"· 约 {len(files)} 个文件，{_fmt_size(nbytes)}\n"
        f"· 覆盖 {passes} 次（不进回收站，不可恢复）"
    )
    if errs:
        warn += f"\n· 另有 {len(errs)} 项将跳过"

    # Prefer an application-modal box that does not raise the hub
    box = QMessageBox(parent)
    box.setWindowTitle("确认粉碎")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText(warn)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    # Avoid activating a buried main window as parent
    if parent is None:
        box.setWindowModality(Qt.WindowModality.ApplicationModal)
    ret = box.exec()
    if ret != QMessageBox.StandardButton.Yes:
        return None

    dlg = QuickShredDialog(
        cleaned,
        passes=passes,
        rename=rename,
        on_announce=on_announce,
        parent=None,  # independent top-level; do not attach to hub
    )
    dlg.start()
    return dlg
