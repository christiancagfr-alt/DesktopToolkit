"""Cross-network file transfer UI (Cloudflare zero-storage signaling/relay)."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from p2p_transfer import P2PSession, make_room_code


class _Bridge(QObject):
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)
    usage = pyqtSignal(str, int, int)  # text, used, limit
    # CF helpers must use signals — QTimer from worker threads never reaches the UI
    cf_msg = pyqtSignal(str)
    cf_detect_done = pyqtSignal(object, object)  # account dict|None, err|None
    cf_list_done = pyqtSignal(object, object)  # items list|None, err|None
    cf_deploy_done = pyqtSignal(object, object)  # url|None, err|None
    cf_btn = pyqtSignal(str, bool)  # which, enabled


class FloatingP2PBoard(QWidget):
    def __init__(self, callbacks=None, state=None, parent=None, *, embedded: bool = False):
        super().__init__(parent)
        self.callbacks = callbacks
        self.state = state if isinstance(state, dict) else {}
        self.embedded = embedded
        self.session: P2PSession | None = None
        self.dragging = False
        self.drag_position = QPoint()
        self._bridge = _Bridge(self)
        self._bridge.status.connect(self._on_status)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.usage.connect(self._on_usage)
        self._bridge.cf_msg.connect(self._cf_set_msg)
        self._bridge.cf_detect_done.connect(self._on_cf_detect_done)
        self._bridge.cf_list_done.connect(self._on_cf_list_done)
        self._bridge.cf_deploy_done.connect(self._on_cf_deploy_done)
        self._bridge.cf_btn.connect(self._on_cf_btn)

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
            self.resize(440, 500)
        self._init_ui()
        self._load()
        self._refresh_usage()

    def _cfg(self) -> dict:
        return self.state.setdefault("p2p", {})

    def _init_ui(self) -> None:
        self.setStyleSheet(
            """
            QFrame#box {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #0f172a, stop:1 #020617);
                border: 1px solid rgba(56,189,248,0.35); border-radius: 16px;
            }
            QLabel { color: #e2e8f0; font-size: 14px; font-weight: 600; }
            QLabel#title { color: #38bdf8; font-size: 18px; font-weight: 800; }
            QLabel#muted { color: #cbd5e1; font-size: 13px; font-weight: 500; }
            QLabel#quotaTitle { color: #e2e8f0; font-size: 14px; font-weight: 800; }
            QLineEdit, QComboBox {
                background: #020617; color: #f8fafc; border: 1px solid #334155;
                border-radius: 8px; padding: 6px 10px; min-height: 28px; font-size: 13px;
            }
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #0ea5e9, stop:1 #0284c7);
                border: none; color: white; font-weight: 800; font-size: 13px;
                padding: 6px 12px; border-radius: 8px; min-height: 30px;
            }
            QPushButton:hover { background: #38bdf8; color: #0c4a6e; }
            QPushButton#soft {
                background: #1e293b; border: 1px solid #475569; color: #f1f5f9;
                font-size: 12px; font-weight: 700; min-height: 28px; padding: 5px 10px;
            }
            QPushButton#danger { background: #dc2626; font-size: 13px; }
            QProgressBar {
                background: #1e293b; border: none; border-radius: 8px; height: 18px;
                text-align: center; color: #f8fafc; font-size: 12px; font-weight: 700;
            }
            QProgressBar::chunk { background: #0ea5e9; border-radius: 8px; }
            QScrollArea { background: transparent; border: none; }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        box = QFrame(objectName="box")
        if self.embedded:
            box.setStyleSheet(
                "QFrame#box { background: transparent; border: none; border-radius: 0; }"
            )
        box_l = QVBoxLayout(box)
        box_l.setContentsMargins(0, 0, 0, 0)
        box_l.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(8 if self.embedded else 14, 8, 8 if self.embedded else 14, 4)
        header.addWidget(QLabel("☁ 跨网点对点传输", objectName="title"), 1)
        if not self.embedded:
            close_btn = QPushButton("×", objectName="soft")
            close_btn.setFixedSize(28, 28)
            close_btn.clicked.connect(self.hide)
            header.addWidget(close_btn)
        box_l.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(8 if self.embedded else 14, 4, 10 if self.embedded else 14, 10)
        lay.setSpacing(6)

        tip = QLabel("Cloudflare 只牵线/转发，不存文件。双方同一握手链接 + 同一房间号即可。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # ---- Cloudflare：紧凑账号 / 握手 ----
        cf_box = QFrame()
        cf_box.setStyleSheet(
            "QFrame { background: #0b1220; border: 1px solid #334155; border-radius: 10px; }"
        )
        cfl = QVBoxLayout(cf_box)
        cfl.setContentsMargins(10, 8, 10, 8)
        cfl.setSpacing(5)
        cfl.addWidget(QLabel("Cloudflare 握手"))
        tip_cf = QLabel(
            "API Token（Account 读 + Workers Scripts 读/写）+ Account ID。"
            "悬停按钮看说明。"
        )
        tip_cf.setObjectName("muted")
        tip_cf.setWordWrap(True)
        tip_cf.setToolTip(
            "Dashboard → My Profile → API Tokens → Create Token\n"
            "模板可用 Edit Cloudflare Workers，或自定义：\n"
            "· Account.Account Settings: Read\n"
            "· Account.Workers Scripts: Read / Edit\n"
            "Account ID 在 Workers 概览右侧。"
        )
        cfl.addWidget(tip_cf)
        tok_row = QHBoxLayout()
        self.txt_cf_token = QLineEdit()
        self.txt_cf_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_cf_token.setPlaceholderText("API Token（Bearer，不是 Global Key）")
        self.txt_cf_account = QLineEdit()
        self.txt_cf_account.setPlaceholderText("Account ID（32 位）")
        tok_row.addWidget(self.txt_cf_token, 2)
        tok_row.addWidget(self.txt_cf_account, 1)
        cfl.addLayout(tok_row)
        cf_btns = QHBoxLayout()
        cf_btns.setSpacing(6)
        self.btn_cf_accounts = QPushButton("检测账号", objectName="soft")
        self.btn_cf_accounts.setToolTip("用 Token 列出账户并自动填入 Account ID")
        self.btn_cf_accounts.clicked.connect(self._cf_detect_account)
        self.btn_cf_list = QPushButton("拉取握手链接", objectName="soft")
        self.btn_cf_list.setToolTip("列出账户下 Worker，并解析 *.workers.dev 握手地址")
        self.btn_cf_list.clicked.connect(self._cf_list_workers)
        self.btn_cf_deploy = QPushButton("一键新建", objectName="primary")
        self.btn_cf_deploy.setToolTip("本机 wrangler deploy cloudflare/ 中转并自动填入")
        self.btn_cf_deploy.clicked.connect(self._cf_deploy_worker)
        cf_btns.addWidget(self.btn_cf_accounts)
        cf_btns.addWidget(self.btn_cf_list)
        cf_btns.addWidget(self.btn_cf_deploy)
        cfl.addLayout(cf_btns)
        pick_row = QHBoxLayout()
        self.cmb_cf_workers = QComboBox()
        self.cmb_cf_workers.setMinimumHeight(30)
        self.cmb_cf_workers.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cmb_cf_workers.setPlaceholderText("选择已有 Worker / 握手链接…")
        self.btn_cf_apply = QPushButton("填入", objectName="soft")
        self.btn_cf_apply.clicked.connect(self._cf_apply_worker)
        pick_row.addWidget(self.cmb_cf_workers, 1)
        pick_row.addWidget(self.btn_cf_apply)
        cfl.addLayout(pick_row)
        self.lbl_cf = QLabel("")
        self.lbl_cf.setObjectName("muted")
        self.lbl_cf.setWordWrap(True)
        cfl.addWidget(self.lbl_cf)
        lay.addWidget(cf_box)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("中转"))
        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("https://xxx.workers.dev")
        from p2p_transfer import DEFAULT_SIGNAL_URL

        if DEFAULT_SIGNAL_URL:
            self.txt_url.setText(DEFAULT_SIGNAL_URL)
        url_row.addWidget(self.txt_url, 1)
        self.btn_test = QPushButton("测试", objectName="soft")
        self.btn_test.clicked.connect(self._test_relay)
        url_row.addWidget(self.btn_test)
        lay.addLayout(url_row)

        quota_box = QFrame()
        quota_box.setStyleSheet(
            "QFrame { background: #0b1220; border: 1px solid #1e3a5f; border-radius: 10px; }"
        )
        qlay = QVBoxLayout(quota_box)
        qlay.setContentsMargins(10, 8, 10, 8)
        qlay.setSpacing(4)
        qhead = QHBoxLayout()
        qtitle = QLabel("今日额度")
        qtitle.setObjectName("quotaTitle")
        qhead.addWidget(qtitle, 1)
        self.btn_usage = QPushButton("刷新", objectName="soft")
        self.btn_usage.clicked.connect(self._refresh_usage)
        qhead.addWidget(self.btn_usage)
        qlay.addLayout(qhead)
        self.quota_bar = QProgressBar()
        self.quota_bar.setRange(0, 100000)
        self.quota_bar.setValue(0)
        self.quota_bar.setFormat("已用 %v / %m")
        self.quota_bar.setTextVisible(True)
        self.quota_bar.setMinimumHeight(20)
        qlay.addWidget(self.quota_bar)
        self.lbl_usage = QLabel("建连计请求；传文件块不计。额度账户共享。")
        self.lbl_usage.setObjectName("muted")
        self.lbl_usage.setWordWrap(True)
        qlay.addWidget(self.lbl_usage)
        lay.addWidget(quota_box)

        room_row = QHBoxLayout()
        room_row.addWidget(QLabel("房间"))
        self.txt_room = QLineEdit()
        self.txt_room.setPlaceholderText("6 位房间码")
        self.txt_room.setMaxLength(8)
        btn_gen = QPushButton("生成", objectName="soft")
        btn_gen.clicked.connect(self._gen_room)
        room_row.addWidget(self.txt_room, 1)
        room_row.addWidget(btn_gen)
        lay.addLayout(room_row)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("接收"))
        self.txt_dest = QLineEdit()
        self.txt_dest.setPlaceholderText("保存目录…")
        btn_dest = QPushButton("浏览", objectName="soft")
        btn_dest.clicked.connect(self._pick_dest)
        path_row.addWidget(self.txt_dest, 1)
        path_row.addWidget(btn_dest)
        lay.addLayout(path_row)

        act = QHBoxLayout()
        act.setSpacing(6)
        self.btn_send = QPushButton("发送文件…")
        self.btn_send.setToolTip("可多选文件；多个文件会打成 zip 再传")
        self.btn_send.clicked.connect(self._send_files)
        self.btn_send_folder = QPushButton("发文件夹", objectName="soft")
        self.btn_send_folder.setToolTip("选择整个文件夹打包发送（zip）")
        self.btn_send_folder.clicked.connect(self._send_folder)
        self.btn_recv = QPushButton("等待接收", objectName="soft")
        self.btn_recv.clicked.connect(self._recv)
        self.btn_stop = QPushButton("停止", objectName="danger")
        self.btn_stop.clicked.connect(self._stop)
        act.addWidget(self.btn_send)
        act.addWidget(self.btn_send_folder)
        act.addWidget(self.btn_recv)
        act.addWidget(self.btn_stop)
        lay.addLayout(act)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        lay.addWidget(self.progress)

        self.lbl_status = QLabel("就绪：检测账号 → 拉取握手链接 → 填入 → 双方同房间号。")
        self.lbl_status.setObjectName("muted")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        help_l = QLabel(
            "顺序：同中转 + 同房间 → 接收方先「等待接收」→ 发送方再发。"
            "超时请「测试」中转，须 durable_rooms=true。"
        )
        help_l.setObjectName("muted")
        help_l.setWordWrap(True)
        lay.addWidget(help_l)
        lay.addStretch(1)
        scroll.setWidget(inner)
        box_l.addWidget(scroll, 1)
        root.addWidget(box)

    def _load(self) -> None:
        cfg = self._cfg()
        from p2p_transfer import DEFAULT_SIGNAL_URL

        if cfg.get("signal_url"):
            self.txt_url.setText(str(cfg["signal_url"]))
        elif DEFAULT_SIGNAL_URL:
            self.txt_url.setText(DEFAULT_SIGNAL_URL)
        if cfg.get("dest_dir"):
            self.txt_dest.setText(str(cfg["dest_dir"]))
        else:
            self.txt_dest.setText(str(Path.home() / "Downloads" / "ParrotP2P"))
        if cfg.get("room"):
            self.txt_room.setText(str(cfg["room"]))
        else:
            self._gen_room()
        if hasattr(self, "txt_cf_token"):
            self.txt_cf_token.setText(str(cfg.get("cf_api_token") or ""))
            self.txt_cf_account.setText(str(cfg.get("cf_account_id") or ""))

    def _save(self) -> None:
        cfg = self._cfg()
        cfg["signal_url"] = self.txt_url.text().strip()
        cfg["dest_dir"] = self.txt_dest.text().strip()
        cfg["room"] = self.txt_room.text().strip().upper()
        if hasattr(self, "txt_cf_token"):
            cfg["cf_api_token"] = self.txt_cf_token.text().strip()
            cfg["cf_account_id"] = self.txt_cf_account.text().strip()
        try:
            if self.callbacks and hasattr(self.callbacks, "save_state"):
                self.callbacks.save_state()
        except Exception:
            pass

    def _cf_creds(self) -> tuple[str, str]:
        from cloudflare_api import normalize_account_id, normalize_token

        token = normalize_token(self.txt_cf_token.text())
        account = normalize_account_id(self.txt_cf_account.text())
        # Keep fields cleaned so next click uses the same values
        if token and self.txt_cf_token.text() != token:
            # don't rewrite password field mid-edit unless only whitespace differed
            if self.txt_cf_token.text().strip() != self.txt_cf_token.text():
                self.txt_cf_token.setText(token)
        if account and normalize_account_id(self.txt_cf_account.text()) == account:
            if self.txt_cf_account.text().strip() != account:
                self.txt_cf_account.setText(account)
        return token, account

    def _cf_set_msg(self, msg: str) -> None:
        self.lbl_cf.setText(msg)
        try:
            self.lbl_status.setText(msg)
        except Exception:
            pass

    def _on_cf_btn(self, which: str, enabled: bool) -> None:
        btn = {
            "accounts": getattr(self, "btn_cf_accounts", None),
            "list": getattr(self, "btn_cf_list", None),
            "deploy": getattr(self, "btn_cf_deploy", None),
        }.get(which)
        if btn is not None:
            btn.setEnabled(bool(enabled))

    def _cf_detect_account(self) -> None:
        token, account = self._cf_creds()
        if not token:
            self._cf_set_msg("请先填写 API Token（不要用 Global API Key）")
            return
        self._cf_set_msg("正在检测账号…")
        self.btn_cf_accounts.setEnabled(False)

        def work() -> None:
            try:
                from cloudflare_api import detect_account

                info = detect_account(token, account)
                self._bridge.cf_detect_done.emit(info, None)
            except Exception as e:
                self._bridge.cf_detect_done.emit(None, str(e))
            finally:
                self._bridge.cf_btn.emit("accounts", True)

        threading.Thread(target=work, daemon=True).start()

    def _on_cf_detect_done(self, info, err) -> None:
        if err:
            self._cf_set_msg(f"检测失败：{err}")
            return
        if not isinstance(info, dict):
            self._cf_set_msg("检测失败：无返回数据")
            return
        acc = info.get("account") or {}
        accounts = info.get("accounts") or []
        names = "、".join(f"{a.get('name')}({str(a.get('id') or '')[:8]}…)" for a in accounts[:5])
        self.txt_cf_account.setText(str(acc.get("id") or ""))
        chosen = f"{acc.get('name')}({str(acc.get('id') or '')[:8]}…)"
        extra = ""
        if len(accounts) > 1:
            extra = f" 已选「{chosen}」（多账户时优先有 Workers 的）。"
        self._cf_set_msg(f"账号 OK：{names}。{extra}正在拉取握手链接…")
        self._save()
        # Chain list on the GUI thread via signal path
        self._cf_list_workers()

    def _cf_list_workers(self) -> None:
        token, account = self._cf_creds()
        if not token:
            self._cf_set_msg("请填写 API Token")
            return
        if not account:
            self._cf_set_msg("请先「检测账号」自动填 Account ID，或手动粘贴")
            return
        self._cf_set_msg("正在拉取握手链接…")
        self.btn_cf_list.setEnabled(False)

        def work() -> None:
            try:
                from cloudflare_api import list_handshake_candidates

                items = list_handshake_candidates(token, account)
                self._bridge.cf_list_done.emit(items, None)
            except Exception as e:
                self._bridge.cf_list_done.emit(None, str(e))
            finally:
                self._bridge.cf_btn.emit("list", True)

        threading.Thread(target=work, daemon=True).start()

    def _on_cf_list_done(self, items, err) -> None:
        if err:
            self._cf_set_msg(f"拉取失败：{err}")
            return
        items = items or []
        self.cmb_cf_workers.clear()
        with_url = 0
        for it in items:
            name = (it or {}).get("name") or ""
            url = (it or {}).get("url") or ""
            label = f"{name}  →  {url}" if url else f"{name}  （无 workers.dev 子域）"
            self.cmb_cf_workers.addItem(label, url or name)
            if url:
                with_url += 1
        if not items:
            self._cf_set_msg("账户下没有 Worker。可点「一键新建」部署握手服务。")
        elif with_url == 0:
            self._cf_set_msg(
                f"已找到 {len(items)} 个 Worker，但没有 workers.dev 子域。"
                "请在 Cloudflare Workers 设置子域，或「一键新建」。"
            )
        else:
            self._cf_set_msg(f"已拉取 {len(items)} 个（{with_url} 个有链接）。选中后点「填入」。")
            for i in range(self.cmb_cf_workers.count()):
                if str(self.cmb_cf_workers.itemData(i) or "").startswith("http"):
                    self.cmb_cf_workers.setCurrentIndex(i)
                    break
        self._save()

    def _cf_apply_worker(self) -> None:
        data = self.cmb_cf_workers.currentData()
        label = self.cmb_cf_workers.currentText().strip()
        url = ""
        name = ""
        if isinstance(data, str) and data.startswith("http"):
            url = data
        elif isinstance(data, str) and data:
            name = data
        if "→" in label:
            name = label.split("→", 1)[0].strip()
        if not url:
            token, account = self._cf_creds()
            if token and account and name:
                try:
                    from cloudflare_api import resolve_handshake_url

                    url = resolve_handshake_url(token, account, name)
                except Exception as e:
                    self._cf_set_msg(f"解析链接失败：{e}")
                    return
        if not url:
            self._cf_set_msg("请先「拉取握手链接」并选择一项")
            return
        self.txt_url.setText(str(url))
        self._cf_set_msg(f"已填入：{url}")
        self._save()

    def _cf_deploy_worker(self) -> None:
        token, account = self._cf_creds()
        tip = (
            "将自动准备 Node.js（若缺失）并用 wrangler 部署 cloudflare/ 握手 Worker。\n"
        )
        if token:
            tip += "已填写 API Token：将用 Token 部署（一般不用打开浏览器）。\n"
        else:
            tip += "未填 Token：首次可能弹出浏览器登录 Cloudflare。\n建议先「检测账号」。\n"
        tip += "\n继续？"
        reply = QMessageBox.question(
            self,
            "一键新建握手服务",
            tip,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._cf_set_msg("正在准备环境并部署…（缺 Node 时会自动下载，可能需几分钟）")
        self.btn_cf_deploy.setEnabled(False)

        def work() -> None:
            try:
                from cloudflare_api import deploy_signaling_worker

                def on_log(m: str) -> None:
                    self._bridge.cf_msg.emit(m.splitlines()[-1][:120] if m else "部署中…")

                url = deploy_signaling_worker(
                    on_log=on_log,
                    api_token=token,
                    account_id=account,
                )
                self._bridge.cf_deploy_done.emit(url, None)
            except Exception as e:
                self._bridge.cf_deploy_done.emit(None, str(e))
            finally:
                self._bridge.cf_btn.emit("deploy", True)

        threading.Thread(target=work, daemon=True).start()

    def _on_cf_deploy_done(self, url, err) -> None:
        if err:
            self._cf_set_msg(f"部署失败：{err}")
            QMessageBox.warning(self, "部署失败", str(err)[:800])
            return
        self.txt_url.setText(str(url or ""))
        self._cf_set_msg(f"部署成功，已填入：{url}")
        self._save()

    def _gen_room(self) -> None:
        self.txt_room.setText(make_room_code(6))

    def _pick_dest(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "接收保存目录", self.txt_dest.text())
        if p:
            self.txt_dest.setText(p)
            self._save()

    def _on_status(self, msg: str) -> None:
        self.lbl_status.setText(msg)
        if msg.startswith("✅"):
            self.progress.setValue(100)
            # Transfer done — refresh remaining quota so user knows if more is OK
            self._refresh_usage()

    def _on_progress(self, cur: int, total: int, name: str) -> None:
        pct = int(cur * 100 / max(1, total))
        self.progress.setValue(pct)
        self.lbl_status.setText(f"传输中 {name} · {cur}/{total} ({pct}%)")

    def _on_usage(self, text: str, used: int, limit: int) -> None:
        self.lbl_usage.setText(text)
        limit = max(1, int(limit or 100_000))
        used = max(0, int(used or 0))
        self.quota_bar.setRange(0, limit)
        self.quota_bar.setValue(min(used, limit))
        rem = max(0, limit - used)
        self.quota_bar.setFormat(f"已用 {used:,} / {limit:,} · 剩余约 {rem:,}")
        self.btn_usage.setEnabled(True)
        self.btn_usage.setText("刷新额度")

    def _refresh_usage(self) -> None:
        token, account = self._cf_creds() if hasattr(self, "txt_cf_token") else ("", "")
        url = self.txt_url.text().strip()
        self.btn_usage.setEnabled(False)
        self.btn_usage.setText("查询中…")
        self.lbl_usage.setText("正在查询额度…")

        def work() -> None:
            # Prefer account-level GraphQL when token present
            if token and account:
                try:
                    from cloudflare_api import fetch_account_workers_usage_today

                    info = fetch_account_workers_usage_today(token, account)
                    used = int(info.get("account_requests_today") or 0)
                    limit = int(info.get("daily_limit") or 100_000)
                    rem = int(info.get("remaining") or max(0, limit - used))
                    line = (
                        f"账户级今日 Workers 调用：已用 {used:,} / {limit:,}，剩余约 {rem:,}\n"
                        f"（UTC 日 {info.get('day_utc')} · {info.get('note')}）"
                    )
                    self._bridge.usage.emit(line, used, limit)
                    return
                except Exception as e:
                    # Fall through to worker /usage
                    err_acc = str(e)
                else:
                    err_acc = ""
            else:
                err_acc = ""

            if not url:
                msg = "请填写中转地址，或先填 Cloudflare Token+Account 查账户额度。"
                if err_acc:
                    msg = f"账户额度查询失败：{err_acc}\n" + msg
                self._bridge.usage.emit(msg, 0, 100_000)
                return
            try:
                from p2p_transfer import fetch_worker_usage, format_usage_line, FREE_DAILY_REQUEST_LIMIT

                info = fetch_worker_usage(url)
                line = format_usage_line(info)
                if err_acc:
                    line = f"账户额度失败（{err_acc}），以下为本 Worker 近似值：\n" + line
                else:
                    line = "本 Worker 近似值（非账户账单）：\n" + line
                used = int(info.get("worker_requests_today") or 0)
                limit = int(info.get("daily_limit") or FREE_DAILY_REQUEST_LIMIT)
                self._bridge.usage.emit(line, used, limit)
            except Exception as e:
                msg = (
                    f"未能读取额度：{e}\n"
                    "账户级请填 Token（需 Account Analytics Read）；"
                    "或部署含 /usage 的 Worker 后查本服务近似值。"
                )
                if err_acc:
                    msg = f"账户：{err_acc}\n" + msg
                self._bridge.usage.emit(msg, 0, 100_000)

        threading.Thread(target=work, daemon=True).start()

    def _test_relay(self) -> None:
        """Ping /health so user can verify Worker URL + Durable Object rooms."""
        url = self.txt_url.text().strip()
        if not url:
            self.lbl_status.setText("请先填写中转地址再测试。")
            return
        self.btn_test.setEnabled(False)
        self.btn_test.setText("测试中…")
        self.lbl_status.setText("正在测试中转…")

        def work() -> None:
            try:
                from p2p_transfer import fetch_worker_health, normalize_http_base

                info = fetch_worker_health(url)
                base = normalize_http_base(url)
                durable = bool(info.get("durable_rooms"))
                if durable:
                    msg = (
                        f"✅ 中转正常：{base}\n"
                        f"房间粘连 durable_rooms=true（可跨网配对）\n"
                        f"名称：{info.get('name') or 'ok'}"
                    )
                else:
                    msg = (
                        f"⚠️ 中转能访问，但 durable_rooms=false\n"
                        f"双方很容易「永远等对方」。\n"
                        f"请在本机 cloudflare/ 目录执行：npx wrangler deploy\n"
                        f"然后再测一次，必须看到 durable_rooms=true。"
                    )
                self._bridge.status.emit(msg)
            except Exception as e:
                self._bridge.status.emit(
                    f"❌ 中转不可用：{e}\n"
                    "请核对地址（如 https://xxx.workers.dev），并确认已 wrangler deploy。"
                )
            finally:
                try:
                    self.btn_test.setEnabled(True)
                    self.btn_test.setText("测试中转")
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    def _session(self) -> P2PSession | None:
        self._save()
        url = self.txt_url.text().strip()
        room = self.txt_room.text().strip()
        if not url:
            self.lbl_status.setText("请先填写 Cloudflare Worker 地址")
            return None
        if len(room) < 4:
            self.lbl_status.setText("房间号至少 4 位")
            return None
        return P2PSession(
            url,
            room,
            on_status=lambda m: self._bridge.status.emit(m),
            on_progress=lambda a, b, c: self._bridge.progress.emit(a, b, c),
        )

    def _start_send(self, paths: list[str]) -> None:
        if not paths:
            return
        sess = self._session()
        if not sess:
            return
        if self.session:
            self.session.stop()
        self.session = sess
        self.progress.setValue(0)
        if len(paths) == 1:
            self.lbl_status.setText(f"准备发送：{Path(paths[0]).name}")
        else:
            self.lbl_status.setText(f"准备发送 {len(paths)} 项（将打包为 zip）…")
        sess.send_paths_async(paths)

    def _send_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件（可多选）")
        self._start_send(list(paths or []))

    def _send_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择要发送的文件夹")
        if path:
            self._start_send([path])

    def _recv(self) -> None:
        dest = self.txt_dest.text().strip() or str(Path.home() / "Downloads")
        sess = self._session()
        if not sess:
            return
        if self.session:
            self.session.stop()
        self.session = sess
        self.progress.setValue(0)
        self.lbl_status.setText("等待对方发送…")
        sess.receive_file_async(dest)

    def _stop(self) -> None:
        if self.session:
            self.session.stop()
            self.session = None
        self.lbl_status.setText("已停止")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.embedded:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.embedded:
            super().mouseMoveEvent(event)
            return
        if self.dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.dragging = False
        super().mouseReleaseEvent(event)
