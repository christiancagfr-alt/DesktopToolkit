"""UI for filename-based file organizer."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from file_organizer import (
    OrganizeOptions,
    execute_moves,
    plan_moves,
    plan_moves_detailed,
    summarize_plans,
)


class _Bridge(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)


class FileOrganizerWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件整理")
        self.resize(720, 620)
        self._plans = []
        self._busy = False
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.finished.connect(self._on_finished)
        self._build()
        self._apply_style()

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        title = QLabel("文件整理")
        title.setObjectName("title")
        lay.addWidget(title)
        tip = QLabel("按文件名规则创建子文件夹并移动文件。建议先「预览」，确认后再「开始整理」。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # folder row
        row = QHBoxLayout()
        row.addWidget(QLabel("源文件夹"))
        self.txt_dir = QLineEdit()
        self.txt_dir.setPlaceholderText("选择要整理的文件夹…")
        row.addWidget(self.txt_dir, 1)
        btn_browse = QPushButton("浏览…", objectName="soft")
        btn_browse.clicked.connect(self._browse)
        row.addWidget(btn_browse)
        lay.addLayout(row)

        # mode
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("分类方式"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("自定义分段（按 - 或 _ 某一段归类）", "custom")
        self.cmb_mode.addItem("前缀（去掉末尾序号）", "prefix")
        self.cmb_mode.addItem("日期（文件名中的日期）", "date")
        self.cmb_mode.addItem("文件类型 / 扩展名", "ext")
        self.cmb_mode.setCurrentIndex(0)  # default: custom
        self.cmb_mode.currentIndexChanged.connect(self._sync_custom_enabled)
        mode_row.addWidget(self.cmb_mode, 1)
        lay.addLayout(mode_row)

        custom = QHBoxLayout()
        custom.addWidget(QLabel("分隔符"))
        self.txt_sep = QLineEdit("-")
        self.txt_sep.setMaximumWidth(48)
        self.txt_sep.setToolTip("按文件名里的分隔符切段，常见为 - 或 _")
        custom.addWidget(self.txt_sep)
        custom.addWidget(QLabel("取法"))
        self.cmb_take = QComboBox()
        self.cmb_take.addItem("仅第 N 段", "nth")
        self.cmb_take.addItem("前 N 段拼在一起", "first")
        self.cmb_take.setToolTip("仅第 N 段：按某一段归类；前 N 段：把前面几段拼成文件夹名")
        custom.addWidget(self.cmb_take)
        custom.addWidget(QLabel("N="))
        self.spin_parts = QSpinBox()
        self.spin_parts.setRange(1, 20)
        self.spin_parts.setValue(3)
        self.spin_parts.setToolTip("段序号从 1 开始")
        custom.addWidget(self.spin_parts)
        custom.addStretch(1)
        lay.addLayout(custom)
        self._custom_row_widgets = (self.txt_sep, self.cmb_take, self.spin_parts)

        opts = QHBoxLayout()
        self.chk_min2 = QCheckBox("仅当同组 ≥ 2 个文件才建文件夹")
        self.chk_min2.setChecked(True)
        self.chk_min2.setToolTip("避免每个单独文件都建一个文件夹")
        opts.addWidget(self.chk_min2)
        self.chk_friendly = QCheckBox("类型用中文名（图片/视频/文档…）")
        self.chk_friendly.setChecked(True)
        opts.addWidget(self.chk_friendly)
        self.chk_recursive = QCheckBox("包含子文件夹中的文件")
        self.chk_recursive.setChecked(False)
        opts.addWidget(self.chk_recursive)
        opts.addStretch(1)
        lay.addLayout(opts)

        btns = QHBoxLayout()
        self.btn_preview = QPushButton("预览", objectName="soft")
        self.btn_preview.clicked.connect(self._preview)
        self.btn_run = QPushButton("开始整理", objectName="primary")
        self.btn_run.clicked.connect(self._run)
        self.btn_open = QPushButton("打开源文件夹", objectName="soft")
        self.btn_open.clicked.connect(self._open_dir)
        btns.addWidget(self.btn_preview)
        btns.addWidget(self.btn_run)
        btns.addWidget(self.btn_open)
        btns.addStretch(1)
        lay.addLayout(btns)

        lay.addWidget(QLabel("预览", objectName="section"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("点「预览」查看将创建的文件夹与移动计划（不会真正移动）")
        lay.addWidget(self.preview, 1)

        self.lbl_status = QLabel("就绪")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        self._sync_custom_enabled()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #111827; color: #e2e8f0; }
            QLabel#title { color: #34d399; font-size: 16px; font-weight: 800; }
            QLabel#section { color: #94a3b8; font-size: 11px; font-weight: 700; }
            QLabel#muted { color: #94a3b8; font-size: 12px; }
            QLineEdit, QSpinBox, QComboBox, QTextEdit {
                background: #0f172a; color: #e2e8f0;
                border: 1px solid #334155; border-radius: 8px; padding: 6px 8px;
            }
            QCheckBox { color: #e2e8f0; spacing: 8px; }
            QPushButton#primary {
                background: #059669; color: white; border: 0; border-radius: 8px;
                padding: 8px 14px; font-weight: 800;
            }
            QPushButton#soft {
                background: #1e293b; color: #e2e8f0; border: 1px solid #475569;
                border-radius: 8px; padding: 8px 12px;
            }
            """
        )

    def _sync_custom_enabled(self) -> None:
        on = self.cmb_mode.currentData() == "custom"
        for w in self._custom_row_widgets:
            w.setEnabled(on)
        self.chk_friendly.setEnabled(self.cmb_mode.currentData() == "ext")

    def _options(self) -> OrganizeOptions:
        return OrganizeOptions(
            mode=str(self.cmb_mode.currentData() or "custom"),
            recursive=bool(self.chk_recursive.isChecked()),
            min_group_size=2 if self.chk_min2.isChecked() else 1,
            friendly_types=bool(self.chk_friendly.isChecked()),
            custom_sep=self.txt_sep.text(),
            custom_parts=int(self.spin_parts.value()),
            custom_take=str(self.cmb_take.currentData() or "nth"),
        )

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择要整理的文件夹")
        if d:
            self.txt_dir.setText(d)

    def _open_dir(self) -> None:
        p = self.txt_dir.text().strip()
        if p and Path(p).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def _preview(self) -> None:
        root = self.txt_dir.text().strip()
        if not root or not Path(root).is_dir():
            QMessageBox.warning(self, "提示", "请先选择有效的源文件夹")
            return
        try:
            detailed = plan_moves_detailed(root, self._options())
        except Exception as e:
            QMessageBox.warning(self, "预览失败", str(e))
            return
        plans = detailed.plans
        self._plans = plans
        groups = summarize_plans(plans)
        lines = []
        lines.append(detailed.message)
        lines.append("")
        if detailed.group_sizes:
            lines.append("识别到的分组（含已在文件夹内的文件）：")
            for g, n in sorted(detailed.group_sizes.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"  · {g}  → {n} 个")
            lines.append("")
        if not plans:
            lines.append("没有需要移动的文件。")
            if detailed.scanned == 0:
                lines.append("提示：当前目录下没有直接可见的文件。若图片在子文件夹里，请勾选「包含子文件夹中的文件」。")
            elif detailed.skipped_small_group:
                lines.append("提示：可取消勾选「仅当同组 ≥ 2 个文件才建文件夹」后再点预览。")
            elif detailed.already_in_place == detailed.scanned:
                lines.append("提示：文件都已在对应命名的子文件夹中，无需再整理。")
        else:
            lines.append(f"将移动到以下文件夹（共 {len(plans)} 个文件）：\n")
            for g, items in groups.items():
                lines.append(f"📁 {g}  （{len(items)} 个）")
                for it in items[:8]:
                    lines.append(f"    {it.src.name}")
                if len(items) > 8:
                    lines.append(f"    …还有 {len(items) - 8} 个")
                lines.append("")
        self.preview.setPlainText("\n".join(lines))
        self.lbl_status.setText(detailed.message + "（尚未移动）")

    def _run(self) -> None:
        if self._busy:
            return
        root = self.txt_dir.text().strip()
        if not root or not Path(root).is_dir():
            QMessageBox.warning(self, "提示", "请先选择有效的源文件夹")
            return
        # Always refresh plan before execute
        try:
            plans = plan_moves(root, self._options())
        except Exception as e:
            QMessageBox.warning(self, "失败", str(e))
            return
        self._plans = plans
        if not plans:
            QMessageBox.information(self, "提示", "没有可移动的文件")
            return
        groups = summarize_plans(plans)
        if (
            QMessageBox.question(
                self,
                "确认整理",
                f"将创建约 {len(groups)} 个文件夹，并移动 {len(plans)} 个文件。\n确定继续？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._busy = True
        self.btn_run.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.lbl_status.setText("正在整理…")

        def work() -> None:
            def prog(i, total, msg):
                self._bridge.progress.emit(f"{i}/{total} {msg}")

            report = execute_moves(plans, on_progress=prog)
            self._bridge.finished.emit(report)

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, msg: str) -> None:
        self.lbl_status.setText(msg)

    def _on_finished(self, report) -> None:
        self._busy = False
        self.btn_run.setEnabled(True)
        self.btn_preview.setEnabled(True)
        err_n = len(report.errors or [])
        self.lbl_status.setText(
            f"完成：新建文件夹 {report.folders_created}，移动 {report.files_moved}，"
            f"跳过/失败 {report.skipped}"
            + (f"（含 {err_n} 条错误）" if err_n else "")
        )
        if report.errors:
            self.preview.append("\n—— 错误 ——\n" + "\n".join(report.errors[:30]))
        QMessageBox.information(
            self,
            "整理完成",
            f"新建文件夹 {report.folders_created} 个\n移动文件 {report.files_moved} 个\n"
            f"失败 {report.skipped} 个",
        )


def show_file_organizer(host=None) -> FileOrganizerWindow:
    win = getattr(host, "file_organizer_win", None) if host is not None else None
    if win is None:
        win = FileOrganizerWindow()
        if host is not None:
            host.file_organizer_win = win
    win.show()
    win.raise_()
    win.activateWindow()
    return win
