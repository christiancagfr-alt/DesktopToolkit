"""Floating cleaner board: scopes, settings, logs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QPoint, QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from cleaner import ALL_CLEAN_SCOPES, CLEAN_SCOPES, DEFAULT_SCOPES, DEEP_SCOPES, DEEP_SCOPE_IDS


class _CleanBridge(QObject):
    finished = pyqtSignal(object)
    log_line = pyqtSignal(str)


class FloatingCleanerBoard(QWidget):
    """Left nav + right content for PC cleanup."""

    COLLAPSED_H = 40
    COLLAPSED_W = 240

    # Match Desktop Toolkit main dark theme (not light “spliced” look)
    DARK_QSS = """
        QFrame#shell {
            background: #111827; border: 1px solid rgba(99,102,241,0.4);
            border-radius: 12px;
        }
        QFrame#sideNav {
            background: #0f172a;
            border-right: 1px solid rgba(99,102,241,0.28);
            border-top-left-radius: 12px; border-bottom-left-radius: 12px;
        }
        QLabel { color: #e2e8f0; font-weight: 600; }
        QLabel#section { color: #c7d2fe; font-size: 13px; font-weight: 800; }
        QLabel#muted { color: #94a3b8; font-size: 12px; }
        QCheckBox { color: #e2e8f0; spacing: 8px; }
        QPushButton {
            background: #6366f1; color: white; border: none;
            border-radius: 8px; padding: 8px 12px; font-weight: 700;
        }
        QPushButton:hover { background: #818cf8; }
        QPushButton#primary {
            background: #6366f1; color: white; font-weight: 800;
        }
        QPushButton#soft {
            background: #1e293b; color: #e2e8f0; border: 1px solid #475569;
            border-radius: 8px; padding: 8px 14px; font-weight: 700;
        }
        QPushButton#soft:hover { background: #334155; border-color: #64748b; }
        QPushButton#ghost {
            background: #1e293b; color: #e2e8f0; border: 1px solid #475569;
            border-radius: 8px; padding: 8px 14px; font-weight: 700;
        }
        QPushButton#ghost:hover { background: #334155; color: #fff; border-color: #818cf8; }
        QPushButton#navBtn {
            background: transparent; border: none; border-radius: 10px;
            color: #94a3b8; text-align: left; padding: 10px 10px;
            font-size: 12px; font-weight: 600;
        }
        QPushButton#navBtn:hover { background: #1e293b; color: #e2e8f0; }
        QPushButton#navBtn:checked {
            background: #6366f1; color: white; border: 1px solid #818cf8;
        }
        QListWidget {
            background: #0f172a; color: #e2e8f0; border: 1px solid #334155;
            border-radius: 8px; padding: 4px;
        }
        QListWidget::item { padding: 6px; border-radius: 6px; }
        QListWidget::item:selected { background: #312e81; color: #e0e7ff; }
    """

    def __init__(
        self,
        state: dict,
        *,
        on_start_clean: Callable[[list[str]], None],
        on_save_state: Callable[[], None],
        parent: QWidget | None = None,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.state = state
        self.on_start_clean = on_start_clean
        self.on_save_state = on_save_state
        self.embedded = embedded
        self._busy = False
        self._collapsed = bool(self.state.get("clean_board_collapsed")) and not embedded
        self._expand_size = (
            max(480, int(self.state.get("clean_board_w") or 520)),
            max(420, int(self.state.get("clean_board_h") or 480)),
        )
        self._scope_checks: dict[str, QCheckBox] = {}

        self.setWindowTitle("清理电脑")
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        else:
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(self.COLLAPSED_W, self.COLLAPSED_H)

        shell = QFrame(self)
        shell.setObjectName("shell")
        shell.setStyleSheet(self.DARK_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Title bar (hidden when embedded in main window)
        self._bar = QFrame()
        self._bar.setStyleSheet(
            "QFrame { background: #1e1b4b; border-top-left-radius: 12px; border-top-right-radius: 12px; "
            "border-bottom: 1px solid rgba(99,102,241,0.35); }"
            "QLabel { color: #c7d2fe; font-weight: 800; }"
            "QPushButton { background: transparent; border: none; color: #e0e7ff; font-weight: 800; min-width: 28px; }"
            "QPushButton:hover { color: #fca5a5; }"
        )
        bar_l = QHBoxLayout(self._bar)
        bar_l.setContentsMargins(10, 6, 8, 6)
        bar_l.addWidget(QLabel("🧹 清理电脑"), 1)
        self._btn_collapse = QPushButton("▼")
        self._btn_collapse.setToolTip("折叠 / 展开")
        self._btn_collapse.clicked.connect(self.toggle_collapse)
        bar_l.addWidget(self._btn_collapse)
        btn_close = QPushButton("×")
        btn_close.clicked.connect(self.hide)
        bar_l.addWidget(btn_close)
        root.addWidget(self._bar)
        if embedded:
            self._bar.hide()

        self.body_widget = QWidget()
        body = QVBoxLayout(self.body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)

        side = QFrame()
        side.setObjectName("sideNav")
        side.setFixedWidth(108)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(8, 12, 8, 12)
        side_l.setSpacing(6)
        side_l.addWidget(QLabel("板块"))
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_btns: list[QPushButton] = []
        for i, label in enumerate(("🧹  范围", "⚙  设置", "📋  日志")):
            btn = QPushButton(label)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._nav_group.addButton(btn, i)
            side_l.addWidget(btn)
            self._nav_btns.append(btn)
        side_l.addStretch(1)
        self._nav_group.idClicked.connect(self._on_nav)
        mid.addWidget(side)

        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self._build_scope_page())
        self.page_stack.addWidget(self._build_settings_page())
        self.page_stack.addWidget(self._build_log_page())
        mid.addWidget(self.page_stack, 1)
        body.addLayout(mid, 1)

        # Fixed bottom action
        bottom = QHBoxLayout()
        bottom.setContentsMargins(12, 8, 12, 12)
        self.lbl_status = QLabel("选择清理范围后点「开始清理」。")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        self.btn_deep = QPushButton("深度清理")
        self.btn_deep.setObjectName("soft")
        self.btn_deep.setMinimumHeight(36)
        self.btn_deep.setToolTip("勾选全部常规项 + 深度项（错误报告/崩溃转储/GPU缓存等），更彻底")
        self.btn_deep.clicked.connect(self._start_deep)
        self.btn_run = QPushButton("开始清理")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(36)
        self.btn_run.clicked.connect(self._start)
        bottom.addWidget(self.lbl_status, 1)
        bottom.addWidget(self.btn_deep)
        bottom.addWidget(self.btn_run)
        body.addLayout(bottom)

        root.addWidget(self.body_widget, 1)
        self._nav_btns[0].setChecked(True)

        self._grip = QWidget(self)
        self._grip.setFixedSize(16, 16)
        self._grip.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._grip.setStyleSheet("background: #6366f1; border-radius: 3px;")
        self._resize_origin: QPoint | None = None
        self._resize_size: tuple[int, int] | None = None
        self._grip.mousePressEvent = self._grip_press  # type: ignore
        self._grip.mouseMoveEvent = self._grip_move  # type: ignore
        self._grip.mouseReleaseEvent = self._grip_release  # type: ignore
        if embedded:
            self._grip.hide()

        if not embedded:
            try:
                if self.state.get("clean_board_x") is not None:
                    self.move(int(self.state["clean_board_x"]), int(self.state["clean_board_y"]))
            except (TypeError, ValueError):
                pass
            if self._collapsed:
                self._apply_collapsed(True, persist=False)
            else:
                self.resize(*self._expand_size)
            try:
                from ui_theme import clamp_to_screens

                clamp_to_screens(self)
            except Exception:
                pass
        self._load_logs()

    def _clean_cfg(self) -> dict:
        return self.state.setdefault("cleaner", {})

    def _build_scope_page(self) -> QWidget:
        """Compact checklist: one row per item (tooltip = detail), scrollable."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 10, 12, 8)
        outer.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("清理范围")
        title.setObjectName("section")
        head.addWidget(title, 1)
        tip = QLabel("悬停查看说明 · 敏感项请确认后再勾")
        tip.setObjectName("muted")
        head.addWidget(tip)
        outer.addLayout(head)

        saved = self._clean_cfg().get("scopes")
        if not isinstance(saved, list) or not saved:
            saved = list(DEFAULT_SCOPES)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(4, 0, 8, 0)
        lay.setSpacing(4)

        def _add_group(heading: str, scopes: list) -> None:
            h = QLabel(heading)
            h.setObjectName("section")
            h.setStyleSheet("QLabel#section { padding-top: 6px; }")
            lay.addWidget(h)
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #0f172a; border: 1px solid #334155; border-radius: 10px; }"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(10, 8, 10, 8)
            cl.setSpacing(2)
            for sid, label, desc in scopes:
                box = QCheckBox(label)
                box.setChecked(sid in saved)
                box.setToolTip(desc)
                box.setMinimumHeight(26)
                box.stateChanged.connect(self._persist_scopes)
                self._scope_checks[sid] = box
                cl.addWidget(box)
            lay.addWidget(card)

        _add_group("常规", list(CLEAN_SCOPES))
        if DEEP_SCOPES:
            _add_group("深度（更彻底）", list(DEEP_SCOPES))
        lay.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        all_btn = QPushButton("全部勾选")
        all_btn.setObjectName("soft")
        all_btn.setMinimumHeight(32)
        all_btn.clicked.connect(lambda: self._set_all_scopes(True))
        none_btn = QPushButton("全部取消")
        none_btn.setObjectName("soft")
        none_btn.setMinimumHeight(32)
        none_btn.clicked.connect(lambda: self._set_all_scopes(False))
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        outer.addLayout(row)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(12)
        title = QLabel("清理设置")
        title.setObjectName("section")
        lay.addWidget(title)

        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #0f172a; border: 1px solid #334155; border-radius: 10px; }"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(10)
        cfg = self._clean_cfg()
        self.chk_speak = QCheckBox("清理完成后语音播报")
        self.chk_speak.setChecked(bool(cfg.get("speak", True)))
        self.chk_speak.stateChanged.connect(self._persist_settings)
        self.chk_bubble = QCheckBox("完成后弹出气泡提示")
        self.chk_bubble.setChecked(bool(cfg.get("bubble", True)))
        self.chk_bubble.stateChanged.connect(self._persist_settings)
        self.chk_keep_log = QCheckBox("写入清理日志（建议开启）")
        self.chk_keep_log.setChecked(bool(cfg.get("keep_log", True)))
        self.chk_keep_log.stateChanged.connect(self._persist_settings)
        for w in (self.chk_speak, self.chk_bubble, self.chk_keep_log):
            w.setMinimumHeight(28)
            cl.addWidget(w)
        tip = QLabel("后台执行，不卡界面；被占用的文件会自动跳过。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        cl.addWidget(tip)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_log_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("清理日志")
        title.setObjectName("section")
        head.addWidget(title)
        head.addStretch(1)
        clear = QPushButton("清空日志")
        clear.setObjectName("ghost")
        clear.clicked.connect(self._clear_logs)
        head.addWidget(clear)
        lay.addLayout(head)
        self.log_list = QListWidget()
        lay.addWidget(self.log_list, 1)
        return page

    def _on_nav(self, index: int) -> None:
        if 0 <= index < self.page_stack.count():
            self.page_stack.setCurrentIndex(index)
            if index == 2:
                self._load_logs()

    def selected_scopes(self) -> list[str]:
        return [sid for sid, box in self._scope_checks.items() if box.isChecked()]

    def _set_all_scopes(self, checked: bool) -> None:
        for box in self._scope_checks.values():
            box.setChecked(checked)
        self._persist_scopes()

    def _persist_scopes(self) -> None:
        self._clean_cfg()["scopes"] = self.selected_scopes()
        try:
            self.on_save_state()
        except Exception:
            pass

    def _persist_settings(self) -> None:
        cfg = self._clean_cfg()
        cfg["speak"] = bool(self.chk_speak.isChecked())
        cfg["bubble"] = bool(self.chk_bubble.isChecked())
        cfg["keep_log"] = bool(self.chk_keep_log.isChecked())
        try:
            self.on_save_state()
        except Exception:
            pass

    def _start(self) -> None:
        if self._busy:
            self.lbl_status.setText("正在清理中，请稍候…")
            return
        scopes = self.selected_scopes()
        if not scopes:
            self.lbl_status.setText("请至少勾选一项清理范围。")
            return
        self._busy = True
        self.btn_run.setEnabled(False)
        self.btn_deep.setEnabled(False)
        self.lbl_status.setText("正在清理，请稍候…")
        try:
            self.on_start_clean(scopes)
        except Exception as exc:
            self._busy = False
            self.btn_run.setEnabled(True)
            self.btn_deep.setEnabled(True)
            self.lbl_status.setText(f"启动失败：{exc}")

    def _start_deep(self) -> None:
        """Select all normal + deep scopes, confirm, then clean."""
        if self._busy:
            self.lbl_status.setText("正在清理中，请稍候…")
            return
        from PyQt6.QtWidgets import QMessageBox

        if (
            QMessageBox.question(
                self,
                "深度清理",
                "将清理全部常规项，并额外清理：\n"
                "错误报告、崩溃转储、GPU 着色器缓存、最近使用记录、聊天软件缓存、日志。\n\n"
                "不会删除你的文档/照片。被占用的文件会自动跳过。\n确定继续？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        # Check all scopes in UI
        for sid, box in self._scope_checks.items():
            box.blockSignals(True)
            box.setChecked(True)
            box.blockSignals(False)
        self._persist_scopes()
        scopes = list(DEFAULT_SCOPES) + list(DEEP_SCOPE_IDS)
        self._busy = True
        self.btn_run.setEnabled(False)
        self.btn_deep.setEnabled(False)
        self.lbl_status.setText("正在深度清理，请稍候…")
        try:
            self.on_start_clean(scopes)
        except Exception as exc:
            self._busy = False
            self.btn_run.setEnabled(True)
            self.btn_deep.setEnabled(True)
            self.lbl_status.setText(f"启动失败：{exc}")

    def on_clean_result(self, summary: str, *, details: str = "") -> None:
        """Call from main thread when clean finishes."""
        self._busy = False
        self.btn_run.setEnabled(True)
        try:
            self.btn_deep.setEnabled(True)
        except Exception:
            pass
        self.lbl_status.setText(summary)
        cfg = self._clean_cfg()
        if cfg.get("keep_log", True):
            logs = cfg.setdefault("logs", [])
            if not isinstance(logs, list):
                logs = []
                cfg["logs"] = logs
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": summary,
                "details": details,
                "scopes": list(self.selected_scopes()),
            }
            logs.insert(0, entry)
            del logs[80:]
            try:
                self.on_save_state()
            except Exception:
                pass
            self._load_logs()

    def _load_logs(self) -> None:
        if not hasattr(self, "log_list"):
            return
        self.log_list.clear()
        logs = self._clean_cfg().get("logs") or []
        if not logs:
            self.log_list.addItem("（暂无清理记录）")
            return
        for entry in logs:
            if not isinstance(entry, dict):
                continue
            t = entry.get("time") or ""
            s = entry.get("summary") or ""
            scopes = entry.get("scopes") or []
            sc = "、".join(str(x) for x in scopes[:6]) if scopes else ""
            item = QListWidgetItem(f"{t}\n{s}" + (f"\n范围：{sc}" if sc else ""))
            item.setToolTip(str(entry.get("details") or s))
            self.log_list.addItem(item)

    def _clear_logs(self) -> None:
        self._clean_cfg()["logs"] = []
        try:
            self.on_save_state()
        except Exception:
            pass
        self._load_logs()
        self.lbl_status.setText("日志已清空。")

    # --- collapse / geometry ---
    def toggle_collapse(self) -> None:
        self._apply_collapsed(not self._collapsed, persist=True)

    def _apply_collapsed(self, collapsed: bool, *, persist: bool = True) -> None:
        self._collapsed = collapsed
        if collapsed:
            if self.body_widget.isVisible() and self.height() > self.COLLAPSED_H + 20:
                self._expand_size = (max(self.COLLAPSED_W, self.width()), max(420, self.height()))
            self.body_widget.hide()
            self._grip.hide()
            self.setMinimumSize(self.COLLAPSED_W, self.COLLAPSED_H)
            self.setMaximumHeight(self.COLLAPSED_H + 4)
            self.resize(max(self.COLLAPSED_W, min(self.width(), 320)), self.COLLAPSED_H)
            self._btn_collapse.setText("▶")
            self._bar.title_label.setText("🧹 清理" + (" · 进行中" if self._busy else ""))
        else:
            self.setMaximumHeight(16777215)
            self.setMinimumSize(420, 400)
            self.body_widget.show()
            self._grip.show()
            self.resize(*self._expand_size)
            self._btn_collapse.setText("▼")
            self._bar.title_label.setText("🧹 清理电脑")
        if persist:
            self._persist_geometry()

    def _persist_geometry(self) -> None:
        self.state["clean_board_x"] = int(self.x())
        self.state["clean_board_y"] = int(self.y())
        self.state["clean_board_collapsed"] = bool(self._collapsed)
        if not self._collapsed:
            self.state["clean_board_w"] = int(self.width())
            self.state["clean_board_h"] = int(self.height())
            self._expand_size = (self.width(), self.height())
        try:
            self.on_save_state()
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if not self._collapsed:
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
        self.resize(max(420, self._resize_size[0] + delta.x()), max(400, self._resize_size[1] + delta.y()))
        event.accept()

    def _grip_release(self, event) -> None:
        self._resize_origin = None
        self._resize_size = None
        if not self._collapsed:
            self._expand_size = (self.width(), self.height())
            self._persist_geometry()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self._collapsed and event.button() == Qt.MouseButton.LeftButton:
            self._apply_collapsed(False, persist=True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
