"""Evernote-style notebook window (independent from sticky notes)."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize, QUrl, QPoint, QRect
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
    QTextImageFormat,
    QTextListFormat,
)
from PyQt6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from notebook_store import NotebookStore

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.I)
_URL_IN_TEXT_RE = re.compile(r"(https?://[^\s<>\"']+)", re.I)

_TEXT_COLORS = (
    ("红", "#ef4444"),
    ("橙", "#f97316"),
    ("黄", "#eab308"),
    ("绿", "#22c55e"),
    ("青", "#06b6d4"),
    ("蓝", "#3b82f6"),
    ("紫", "#a855f7"),
    ("粉", "#ec4899"),
    ("白", "#f8fafc"),
    ("黑", "#0f172a"),
)
_HIGHLIGHT_COLORS = (
    ("无", ""),
    ("黄", "#fef08a"),
    ("绿", "#bbf7d0"),
    ("蓝", "#bfdbfe"),
    ("粉", "#fbcfe8"),
    ("紫", "#e9d5ff"),
    ("红", "#fecaca"),
    ("橙", "#fed7aa"),
)


def _normalize_http_url(url: str) -> str | None:
    """Parse and validate an http(s) URL. Returns cleaned URL or None on failure."""
    from urllib.parse import urlparse, urlunparse

    raw = (url or "").strip()
    if not raw:
        return None
    # Strip common wrappers / control chars that break parsing
    raw = raw.strip(" \t\r\n<>\"'`")
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw)
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        u = urlparse(raw)
    except Exception:
        return None
    scheme = (u.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    host = (u.hostname or "").lower().strip(".")
    if not host:
        return None
    # Reject obviously invalid hosts
    if " " in host or host.startswith(".") or ".." in host:
        return None
    # Rebuild without credentials / fragments that shouldn't be stored in notes as-is
    try:
        cleaned = urlunparse(
            (scheme, host + (f":{u.port}" if u.port else ""), u.path or "", "", u.query or "", "")
        )
    except Exception:
        return None
    return cleaned


def _host_matches(host: str, *domains: str) -> bool:
    """True if host is exactly domain or a subdomain (not a substring spoof)."""
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for domain in domains:
        d = (domain or "").lower().strip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_public_http_url(url: str) -> bool:
    """Reject loopback/private/link-local destinations, including DNS answers."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    cleaned = _normalize_http_url(url)
    if not cleaned:
        return False
    host = urlparse(cleaned).hostname or ""
    try:
        answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for answer in answers:
        try:
            ip = ipaddress.ip_address(answer[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(answers)


def fetch_link_title(url: str) -> str:
    """Best-effort page/sheet title for hyperlink display text."""
    import html as html_lib
    import urllib.request

    cleaned = _normalize_http_url(url)
    if not cleaned or not _is_public_http_url(cleaned):
        return ""
    try:
        class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                if not _is_public_http_url(newurl):
                    raise ValueError("拒绝跳转到本机或私有网络地址")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        req = urllib.request.Request(
            cleaned,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        opener = urllib.request.build_opener(PublicRedirectHandler())
        with opener.open(req, timeout=8) as resp:
            raw = resp.read(120_000)
        text = raw.decode("utf-8", errors="replace")
        title = ""
        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            text,
            re.I,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                text,
                re.I,
            )
        if m:
            title = m.group(1)
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            if m:
                title = m.group(1)
        title = html_lib.unescape(re.sub(r"\s+", " ", title or "")).strip()
        for suffix in (
            " - Google Sheets",
            " - Google 表格",
            " - Google Docs",
            " - Google 文档",
            " - Google Drive",
            " - Google 云端硬盘",
            " - YouTube",
            " | Notion",
        ):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
        # Google often returns a generic login title when private
        if title.lower() in {"google sheets", "google 表格", "google docs", "google drive"}:
            return ""
        return title[:120]
    except Exception:
        return ""


def _guess_link_label(url: str) -> str:
    """Offline fallback label when title fetch fails."""
    from urllib.parse import urlparse, unquote

    cleaned = _normalize_http_url(url)
    if not cleaned:
        return (url or "")[:80] or "链接"
    try:
        u = urlparse(cleaned)
    except Exception:
        return cleaned[:80]
    host = (u.hostname or "").lower()
    path = unquote(u.path or "")
    path_parts = [p for p in path.split("/") if p]

    if _host_matches(host, "docs.google.com"):
        if path_parts and path_parts[0] == "spreadsheets":
            return "Google 表格"
        if path_parts and path_parts[0] == "document":
            return "Google 文档"
        if path_parts and path_parts[0] == "presentation":
            return "Google 幻灯片"
        return "Google Docs"
    if _host_matches(host, "drive.google.com"):
        return "Google Drive 文件"
    if _host_matches(host, "youtube.com", "youtu.be"):
        return "YouTube 视频"
    name = path.rstrip("/").split("/")[-1] if path else ""
    if name and "." in name and len(name) < 60:
        return name
    return host or cleaned[:80]

def _html_to_markdown_simple(html: str) -> str:
    text = html or ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)<h1[^>]*>(.*?)</h1>", r"# \1\n\n", text, flags=re.S)
    text = re.sub(r"(?i)<h2[^>]*>(.*?)</h2>", r"## \1\n\n", text, flags=re.S)
    text = re.sub(r"(?i)<h3[^>]*>(.*?)</h3>", r"### \1\n\n", text, flags=re.S)
    text = re.sub(r"(?i)<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.S)
    text = re.sub(r"(?i)<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.S)
    text = re.sub(r"(?i)<b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.S)
    text = re.sub(r"(?i)<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.S)
    text = re.sub(r"(?i)<i[^>]*>(.*?)</i>", r"*\1*", text, flags=re.S)
    text = re.sub(r"(?i)<img[^>]+src=['\"]([^'\"]+)['\"][^>]*>", r"![](\1)", text)
    text = re.sub(
        r"(?i)<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        r"[\2](\1)",
        text,
        flags=re.S,
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&amp;", "&", text)
    return text.strip() + ("\n" if text.strip() else "")


def _markdown_to_html_simple(md: str) -> str:
    lines = (md or "").splitlines()
    out: list[str] = []
    in_ul = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_ul()
            continue
        if line.startswith("### "):
            close_ul()
            out.append(f"<h3>{_inline_md(line[4:])}</h3>")
        elif line.startswith("## "):
            close_ul()
            out.append(f"<h2>{_inline_md(line[3:])}</h2>")
        elif line.startswith("# "):
            close_ul()
            out.append(f"<h1>{_inline_md(line[2:])}</h1>")
        elif re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline_md(re.sub(r'^[-*]\\s+', '', line))}</li>")
        elif re.match(r"^!\[.*?\]\((.+?)\)$", line.strip()):
            close_ul()
            m = re.match(r"^!\[.*?\]\((.+?)\)$", line.strip())
            src = m.group(1) if m else ""
            out.append(f'<p><img src="{src}" /></p>')
        else:
            close_ul()
            out.append(f"<p>{_inline_md(line)}</p>")
    close_ul()
    return "\n".join(out)


def _inline_md(s: str) -> str:
    # Extract markdown links/images before escaping
    holders: list[str] = []

    def hold_img(m: re.Match) -> str:
        holders.append(f'<img alt="{m.group(1)}" src="{m.group(2)}" />')
        return f"\x00H{len(holders) - 1}\x00"

    def hold_link(m: re.Match) -> str:
        holders.append(f'<a href="{m.group(2)}">{m.group(1)}</a>')
        return f"\x00H{len(holders) - 1}\x00"

    s = re.sub(r"!\[(.*?)\]\((.+?)\)", hold_img, s)
    s = re.sub(r"\[(.+?)\]\((.+?)\)", hold_link, s)
    s = (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    for i, html in enumerate(holders):
        s = s.replace(f"\x00H{i}\x00", html)
    return s


class _RichEdit(QTextEdit):
    """QTextEdit: image/URL paste, Ctrl+Click links, drag-corner image resize."""

    image_pasted = pyqtSignal(bytes, str)
    link_pasted = pyqtSignal(str)

    _HANDLE = 14  # hit area for bottom-right resize corner (px)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.document().setDefaultStyleSheet(
            "a { color: #38bdf8; text-decoration: underline; }"
        )
        self.setMouseTracking(True)
        self._img_resize: dict[str, Any] | None = None
        self._img_hover_rect: Any = None  # QRect for paint handle

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        if source is None:
            return
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                from PyQt6.QtCore import QBuffer, QIODevice

                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                img.save(buf, "PNG")
                self.image_pasted.emit(bytes(buf.data()), "paste.png")
                return
        if source.hasUrls():
            http_urls = []
            for url in source.urls():
                path = url.toLocalFile()
                if path and Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    try:
                        data = Path(path).read_bytes()
                        self.image_pasted.emit(data, Path(path).name)
                        return
                    except Exception:
                        pass
                s = url.toString().strip()
                try:
                    from urllib.parse import urlparse

                    pu = urlparse(s)
                    if (pu.scheme or "").lower() in {"http", "https"} and (pu.hostname or ""):
                        http_urls.append(s)
                except Exception:
                    pass
            if http_urls:
                for u in http_urls:
                    self.link_pasted.emit(u)
                return
        if source.hasText():
            text = (source.text() or "").strip()
            # Pure URL → hyperlink
            if _URL_RE.match(text):
                self.link_pasted.emit(text)
                return
            # Multiline / mixed: linkify bare URLs
            if _URL_IN_TEXT_RE.search(text) and "<" not in text:
                parts = _URL_IN_TEXT_RE.split(text)
                cursor = self.textCursor()
                for part in parts:
                    if _URL_RE.match(part.strip()):
                        self.link_pasted.emit(part.strip())
                    elif part:
                        cursor.insertText(part)
                return
        super().insertFromMimeData(source)

    def _image_natural_size(self, fmt: QTextImageFormat) -> tuple[int, int]:
        w = int(fmt.width())
        h = int(fmt.height())
        if w > 0 and h > 0:
            return w, h
        name = (fmt.name() or "").strip()
        local = QUrl(name).toLocalFile() if name else ""
        pix = QPixmap(local) if local else QPixmap()
        if pix.isNull() and name:
            pix = QPixmap(name)
        if not pix.isNull():
            return max(1, pix.width()), max(1, pix.height())
        return max(1, w or 200), max(1, h or 150)

    def _find_image_fragment_at(self, pos: QPoint) -> tuple[QTextCursor, QTextImageFormat, Any] | None:
        """Return (cursor_on_image, format, viewport QRect) or None."""
        cursor = self.cursorForPosition(pos)
        candidates = [cursor]
        c_left = QTextCursor(cursor)
        if c_left.movePosition(QTextCursor.MoveOperation.Left):
            candidates.append(c_left)
        for c in candidates:
            fmt = c.charFormat()
            if not fmt.isImageFormat():
                continue
            img_fmt = fmt.toImageFormat()
            nw, nh = self._image_natural_size(img_fmt)
            # Place cursor at image char; cursorRect ≈ top-left of inline object
            c_img = QTextCursor(c)
            # Ensure we are on the image character
            if not c_img.charFormat().isImageFormat():
                continue
            top_left = self.cursorRect(c_img).topLeft()
            rect = QRect(top_left.x(), top_left.y(), int(nw), int(nh))
            # cursorRect can be narrow; expand using format size
            if rect.width() < 8:
                rect.setWidth(int(nw))
            if rect.height() < 8:
                rect.setHeight(int(nh))
            if rect.contains(pos) or rect.adjusted(-4, -4, 4, 4).contains(pos):
                return c_img, img_fmt, rect
        return None

    def _hit_resize_handle(self, pos: QPoint) -> dict[str, Any] | None:
        hit = self._find_image_fragment_at(pos)
        if not hit:
            return None
        cursor, img_fmt, rect = hit
        handle = rect.adjusted(
            rect.width() - self._HANDLE, rect.height() - self._HANDLE, 0, 0
        )
        # enlarge hit box a bit
        handle = handle.adjusted(-4, -4, 6, 6)
        if not handle.contains(pos):
            # still allow selecting image body for showing handle
            if rect.contains(pos):
                self._img_hover_rect = rect
                self.viewport().update()
            return None
        nw, nh = self._image_natural_size(img_fmt)
        aspect = (nw / nh) if nh else 1.0
        return {
            "cursor_pos": cursor.position(),
            "start_w": float(nw),
            "start_h": float(nh),
            "aspect": aspect,
            "origin": pos,
            "name": img_fmt.name(),
        }

    def _apply_image_resize(self, pos: QPoint) -> None:
        drag = self._img_resize
        if not drag:
            return
        dx = pos.x() - drag["origin"].x()
        new_w = max(48.0, drag["start_w"] + dx)
        new_h = max(48.0, new_w / max(0.05, float(drag["aspect"])))
        # Clamp extreme sizes
        new_w = min(new_w, 4000.0)
        new_h = min(new_h, 4000.0)
        c = self.textCursor()
        c.setPosition(int(drag["cursor_pos"]))
        fmt = c.charFormat()
        if not fmt.isImageFormat():
            # try left
            c.movePosition(QTextCursor.MoveOperation.Left)
            fmt = c.charFormat()
        if not fmt.isImageFormat():
            return
        img = fmt.toImageFormat()
        if drag.get("name"):
            img.setName(str(drag["name"]))
        img.setWidth(new_w)
        img.setHeight(new_h)
        # Select the single image character and reapply format
        c.setPosition(int(drag["cursor_pos"]))
        if not c.charFormat().isImageFormat():
            c.movePosition(QTextCursor.MoveOperation.Left)
        c.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
        if not c.charFormat().isImageFormat():
            c.setPosition(int(drag["cursor_pos"]))
            c.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor)
        c.mergeCharFormat(img)
        c.clearSelection()
        self.setTextCursor(c)
        # Keep hover rect updated for handle paint
        hit = self._find_image_fragment_at(pos)
        self._img_hover_rect = hit[2] if hit else None
        self.viewport().update()

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton and not (
            e.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            hit = self._hit_resize_handle(e.position().toPoint())
            if hit:
                self._img_resize = hit
                self.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor)
                e.accept()
                return
            # Click on image body → show handle
            found = self._find_image_fragment_at(e.position().toPoint())
            self._img_hover_rect = found[2] if found else None
            self.viewport().update()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._img_resize is not None and e.button() == Qt.MouseButton.LeftButton:
            self._img_resize = None
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and (
            e.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            anchor = self.anchorAt(e.position().toPoint())
            if anchor:
                QDesktopServices.openUrl(QUrl(anchor))
                e.accept()
                return
        super().mouseReleaseEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        pos = e.position().toPoint()
        if self._img_resize is not None:
            self._apply_image_resize(pos)
            e.accept()
            return
        # Resize handle hover
        hit = self._hit_resize_handle(pos)
        if hit:
            self.viewport().setCursor(Qt.CursorShape.SizeFDiagCursor)
            self.setToolTip("拖动右下角等比缩放图片")
            found = self._find_image_fragment_at(pos)
            self._img_hover_rect = found[2] if found else None
            self.viewport().update()
            e.accept()
            return
        found = self._find_image_fragment_at(pos)
        if found:
            self._img_hover_rect = found[2]
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self.setToolTip("拖动图片右下角可等比缩放")
            self.viewport().update()
        else:
            if self._img_hover_rect is not None:
                self._img_hover_rect = None
                self.viewport().update()
            anchor = self.anchorAt(pos)
            if anchor:
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
                self.setToolTip(f"Ctrl+单击打开：{anchor}")
            else:
                self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
                self.setToolTip("Ctrl+单击可打开超链接；图片可拖右下角等比缩放")
        super().mouseMoveEvent(e)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        rect = self._img_hover_rect
        if rect is None and self._img_resize is None:
            return
        r = rect
        if r is None:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # light border around selected/hovered image
        p.setPen(QPen(QColor("#38bdf8"), 1, Qt.PenStyle.DashLine))
        p.drawRect(r.adjusted(0, 0, -1, -1))
        # bottom-right handle
        hs = 10
        hr = QRect(r.right() - hs, r.bottom() - hs, hs, hs)
        p.setBrush(QColor("#38bdf8"))
        p.setPen(QPen(QColor("#0f172a"), 1))
        p.drawRect(hr)
        p.end()

class NotebookWindow(QMainWindow):
    """Three-pane notebook library UI."""

    def __init__(
        self,
        store: NotebookStore | None = None,
        *,
        app_name: str = "DesktopToolkit",
        get_gdrive_cfg: Callable[[], dict] | None = None,
        save_gdrive_cfg: Callable[[dict], None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("笔记本")
        self.resize(1100, 700)
        self.store = store or NotebookStore(app_name=app_name)
        self.get_gdrive_cfg = get_gdrive_cfg or (lambda: {})
        self.save_gdrive_cfg = save_gdrive_cfg
        self._current_note_id: str | None = None
        self._current_notebook_id = "__all__"
        self._current_tag: str | None = None
        self._loading = False
        self._dirty = False
        self._sync_busy = False

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_save)

        self._build_ui()
        self._apply_style()
        self.refresh_sidebar()
        self.refresh_note_list()
        # open first note if any
        notes = self.store.list_notes()
        if notes:
            self._open_note(str(notes[0]["id"]))

        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._flush_save)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_note)

    # ---- UI ----
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel("笔记本")
        title.setObjectName("title")
        top.addWidget(title)
        top.addStretch(1)
        self.lbl_status = QLabel("本地库")
        self.lbl_status.setObjectName("muted")
        top.addWidget(self.lbl_status)
        self.btn_sync_up = QPushButton("上传同步", objectName="soft")
        self.btn_sync_up.setToolTip(
            "打包上传到 Google Drive\n"
            "· 账号：与截图共用已连接的 Google 账号\n"
            "· 文件夹：独立「ToolkitNotebook」，不占用截图文件夹 ID"
        )
        self.btn_sync_up.clicked.connect(lambda: self._sync(push=True))
        top.addWidget(self.btn_sync_up)
        self.btn_sync_down = QPushButton("下载合并", objectName="soft")
        self.btn_sync_down.setToolTip(
            "从 Google Drive「ToolkitNotebook」下载备份并合并到本地\n"
            "（与截图文件夹分开；按更新时间，新的覆盖旧的）"
        )
        self.btn_sync_down.clicked.connect(lambda: self._sync(push=False))
        top.addWidget(self.btn_sync_down)
        outer.addLayout(top)

        tip = QLabel(
            "与「便签」互不影响 · 点 A▾ / 高亮▾ 选颜色 · 字号可调 · 粘贴网址自动成链接（Ctrl+单击打开）· "
            "图片可拖右下角等比缩放 · 云同步可选（与截图共用 Google 账号，文件夹独立）"
        )
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        outer.addWidget(tip)

        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # Left: notebooks + tags
        left = QFrame()
        left.setObjectName("panel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 8, 8, 8)
        ll.addWidget(QLabel("笔记本", objectName="section"))
        self.list_nb = QListWidget()
        self.list_nb.currentItemChanged.connect(self._on_nb_selected)
        ll.addWidget(self.list_nb, 2)
        nb_btns = QHBoxLayout()
        b_new_nb = QPushButton("＋", objectName="soft")
        b_new_nb.setToolTip("新建笔记本")
        b_new_nb.clicked.connect(self._new_notebook)
        b_ren_nb = QPushButton("✎", objectName="soft")
        b_ren_nb.setToolTip("重命名")
        b_ren_nb.clicked.connect(self._rename_notebook)
        b_del_nb = QPushButton("🗑", objectName="soft")
        b_del_nb.setToolTip("删除笔记本")
        b_del_nb.clicked.connect(self._delete_notebook)
        nb_btns.addWidget(b_new_nb)
        nb_btns.addWidget(b_ren_nb)
        nb_btns.addWidget(b_del_nb)
        nb_btns.addStretch(1)
        ll.addLayout(nb_btns)
        ll.addWidget(QLabel("标签", objectName="section"))
        self.list_tags = QListWidget()
        self.list_tags.currentItemChanged.connect(self._on_tag_selected)
        ll.addWidget(self.list_tags, 1)
        split.addWidget(left)

        # Middle: note list
        mid = QFrame()
        mid.setObjectName("panel")
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(8, 8, 8, 8)
        search_row = QHBoxLayout()
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("搜索标题 / 正文 / 标签…")
        self.txt_search.textChanged.connect(self._on_search)
        search_row.addWidget(self.txt_search, 1)
        b_new = QPushButton("新建笔记", objectName="primary")
        b_new.clicked.connect(self._new_note)
        search_row.addWidget(b_new)
        ml.addLayout(search_row)
        self.list_notes = QListWidget()
        self.list_notes.setWordWrap(True)
        self.list_notes.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.list_notes.setSpacing(4)
        self.list_notes.currentItemChanged.connect(self._on_note_selected)
        ml.addWidget(self.list_notes, 1)
        note_btns = QHBoxLayout()
        self.btn_pin = QPushButton("置顶", objectName="soft")
        self.btn_pin.setToolTip("置顶 / 取消置顶当前笔记（置顶笔记排在列表最前）")
        self.btn_pin.clicked.connect(self._toggle_pin)
        self.btn_del_note = QPushButton("删除", objectName="danger")
        self.btn_del_note.setToolTip("永久删除当前笔记及其附件")
        self.btn_del_note.clicked.connect(self._delete_note)
        note_btns.addWidget(self.btn_pin)
        note_btns.addWidget(self.btn_del_note)
        note_btns.addStretch(1)
        ml.addLayout(note_btns)
        split.addWidget(mid)

        # Right: editor
        right = QFrame()
        right.setObjectName("panel")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 8, 8, 8)
        head = QHBoxLayout()
        self.txt_title = QLineEdit()
        self.txt_title.setPlaceholderText("标题")
        self.txt_title.textChanged.connect(self._mark_dirty)
        head.addWidget(self.txt_title, 1)
        self.cmb_move = QComboBox()
        self.cmb_move.setToolTip("移动到笔记本")
        self.cmb_move.currentIndexChanged.connect(self._on_move_notebook)
        head.addWidget(self.cmb_move)
        rl.addLayout(head)
        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("标签"))
        self.txt_tags = QLineEdit()
        self.txt_tags.setPlaceholderText("逗号分隔，例如：工作, 会议")
        self.txt_tags.textChanged.connect(self._mark_dirty)
        tag_row.addWidget(self.txt_tags, 1)
        rl.addLayout(tag_row)

        self.tabs = QTabWidget()
        # Rich
        rich_wrap = QWidget()
        rw = QVBoxLayout(rich_wrap)
        rw.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QToolBar()
        self.toolbar.setIconSize(QSize(16, 16))
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for text, tip, slot in (
            ("B", "加粗（选中文字后点击）", self._fmt_bold),
            ("I", "斜体", self._fmt_italic),
            ("H", "标题（较大加粗）", self._fmt_h2),
            ("•", "项目符号列表", self._fmt_list),
        ):
            act = QAction(text, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            self.toolbar.addAction(act)
        self.toolbar.addSeparator()
        # Font size — custom, not only H2
        self.cmb_font_size = QComboBox()
        self.cmb_font_size.setToolTip("文字大小（选中文字后选择）")
        self.cmb_font_size.setMinimumWidth(72)
        for pt, label in (
            (12, "12"),
            (14, "14"),
            (16, "16"),
            (18, "18"),
            (20, "20"),
            (24, "24"),
            (28, "28"),
            (36, "36"),
            (48, "48"),
        ):
            self.cmb_font_size.addItem(label, pt)
        self.cmb_font_size.setCurrentIndex(1)  # 14
        self.cmb_font_size.activated.connect(self._fmt_font_size)
        self.toolbar.addWidget(QLabel(" 字号 "))
        self.toolbar.addWidget(self.cmb_font_size)
        self.toolbar.addSeparator()
        # Visual color buttons (clearer than text menu)
        self.btn_text_color = QToolButton()
        self.btn_text_color.setText("A▾")
        self.btn_text_color.setToolTip("文字颜色（点击弹出色板）")
        self.btn_text_color.setStyleSheet(
            "QToolButton { font-weight: 900; color: #ef4444; padding: 4px 10px; }"
        )
        self.btn_text_color.clicked.connect(lambda: self._show_color_palette(False))
        self.toolbar.addWidget(self.btn_text_color)
        self.btn_highlight = QToolButton()
        self.btn_highlight.setText("高亮▾")
        self.btn_highlight.setToolTip("背景高亮（点击弹出色板）")
        self.btn_highlight.setStyleSheet(
            "QToolButton { background: #fef08a; color: #713f12; font-weight: 700; "
            "border-radius: 6px; padding: 4px 8px; }"
        )
        self.btn_highlight.clicked.connect(lambda: self._show_color_palette(True))
        self.toolbar.addWidget(self.btn_highlight)
        self.toolbar.addSeparator()
        self.toolbar.addWidget(QLabel(" 压缩 "))
        self.cmb_img_size = QComboBox()
        self.cmb_img_size.setToolTip(
            "插入/粘贴时的文件压缩上限（最长边）。\n"
            "插入后可在正文中拖图片右下角，等比缩放显示大小。"
        )
        self.cmb_img_size.setMinimumWidth(88)
        for edge, label in (
            (800, "800px"),
            (1280, "1280px"),
            (1920, "1920px"),
            (0, "原图"),
        ):
            self.cmb_img_size.addItem(label, edge)
        self.cmb_img_size.setCurrentIndex(1)  # 1280 default
        self.toolbar.addWidget(self.cmb_img_size)
        for text, tip, slot in (
            ("🔗", "插入/粘贴超链接", self._insert_link_dialog),
            ("图片", "插入图片：按「压缩」减小体积；正文里可拖右下角等比缩放显示", self._insert_image_file),
            ("附件", "添加附件（图片同样按「压缩」处理）", self._add_attachment),
        ):
            act = QAction(text, self)
            act.setToolTip(tip)
            act.triggered.connect(slot)
            self.toolbar.addAction(act)
        for btn in self.toolbar.findChildren(QToolButton):
            if btn.defaultAction():
                btn.setToolTip(btn.defaultAction().toolTip())
        rw.addWidget(self.toolbar)
        self.rich = _RichEdit()
        self.rich.setAcceptRichText(True)
        self.rich.setMouseTracking(True)
        self.rich.setToolTip("Ctrl+单击可打开超链接；直接粘贴网址会自动变成链接")
        self.rich.image_pasted.connect(self._on_image_pasted)
        self.rich.link_pasted.connect(self._on_link_pasted)
        self.rich.textChanged.connect(self._mark_dirty)
        rw.addWidget(self.rich, 1)
        self.tabs.addTab(rich_wrap, "富文本")

        # Markdown
        md_wrap = QWidget()
        mw = QVBoxLayout(md_wrap)
        mw.setContentsMargins(0, 0, 0, 0)
        md_split = QSplitter(Qt.Orientation.Horizontal)
        self.md_edit = QPlainTextEdit()
        self.md_edit.setPlaceholderText("Markdown… 切换到富文本时会尽力转换（可能损失部分格式）")
        self.md_edit.textChanged.connect(self._on_md_changed)
        self.md_preview = QTextBrowser()
        self.md_preview.setOpenExternalLinks(True)
        md_split.addWidget(self.md_edit)
        md_split.addWidget(self.md_preview)
        md_split.setSizes([400, 300])
        mw.addWidget(md_split, 1)
        md_btns = QHBoxLayout()
        b_img = QPushButton("插入图片", objectName="soft")
        b_img.setToolTip("插入图片文件，生成本地 Markdown 图片语法")
        b_img.clicked.connect(self._insert_image_file)
        b_att = QPushButton("添加附件", objectName="soft")
        b_att.setToolTip("添加附件到当前笔记")
        b_att.clicked.connect(self._add_attachment)
        b_link = QPushButton("插入链接", objectName="soft")
        b_link.setToolTip("插入 Markdown 超链接，并尝试读取网页/表格标题")
        b_link.clicked.connect(self._insert_link_dialog)
        md_btns.addWidget(b_img)
        md_btns.addWidget(b_att)
        md_btns.addWidget(b_link)
        md_btns.addStretch(1)
        mw.addLayout(md_btns)
        self.tabs.addTab(md_wrap, "Markdown")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        rl.addWidget(self.tabs, 1)

        rl.addWidget(QLabel("附件", objectName="section"))
        self.list_att = QListWidget()
        self.list_att.setMaximumHeight(90)
        self.list_att.itemDoubleClicked.connect(self._open_attachment)
        rl.addWidget(self.list_att)
        att_row = QHBoxLayout()
        b_open_att = QPushButton("打开", objectName="soft")
        b_open_att.setToolTip("用系统默认程序打开选中的附件")
        b_open_att.clicked.connect(self._open_selected_attachment)
        b_rm_att = QPushButton("移除附件", objectName="soft")
        b_rm_att.setToolTip("从笔记中移除选中附件（同时删除本地副本）")
        b_rm_att.clicked.connect(self._remove_attachment)
        att_row.addWidget(b_open_att)
        att_row.addWidget(b_rm_att)
        att_row.addStretch(1)
        rl.addLayout(att_row)

        split.addWidget(right)
        split.setSizes([200, 260, 620])

    def _apply_style(self) -> None:
        try:
            from ui_theme import dark_glass_qss, C

            base = dark_glass_qss()
        except Exception:
            C = {"bg1": "#111827", "bg2": "#1e293b", "accent": "#34d399", "text_d": "#f1f5f9", "border_d": "#334155"}
            base = ""
        self.setStyleSheet(
            base
            + f"""
            QMainWindow, QWidget {{ background: {C.get('bg1', '#111827')}; color: {C.get('text_d', '#f1f5f9')}; }}
            QFrame#panel {{
                background: {C.get('bg2', '#1e293b')};
                border: 1px solid {C.get('border_d', '#334155')};
                border-radius: 12px;
            }}
            QListWidget {{
                background: transparent; border: 0; outline: 0;
                color: {C.get('text_d', '#f1f5f9')};
            }}
            /* Selection drawn on the cell widget itself — avoid stacked translucent shadow */
            QListWidget::item {{
                background: transparent; border: none; padding: 2px 0; margin: 0;
            }}
            QListWidget::item:selected {{ background: transparent; }}
            QListWidget::item:hover {{ background: transparent; }}
            QListWidget::item:selected:active {{ background: transparent; }}
            QWidget#noteCell {{
                background: transparent; border: 1px solid transparent; border-radius: 10px;
            }}
            QWidget#noteCell[selected="true"] {{
                background: #1e3a2f;
                border: 1px solid #34d399;
            }}
            QWidget#noteCell[selected="false"]:hover {{
                background: #1e293b;
                border: 1px solid #334155;
            }}
            QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QComboBox {{
                background: rgba(15,23,42,0.85);
                border: 1px solid {C.get('border_d', '#334155')};
                border-radius: 8px;
                padding: 6px 8px;
                color: {C.get('text_d', '#f1f5f9')};
                selection-background-color: #059669;
            }}
            QTabWidget::pane {{ border: 0; }}
            QTabBar::tab {{
                background: rgba(30,41,59,0.9); color: #94a3b8;
                padding: 8px 14px; margin-right: 4px; border-radius: 8px;
            }}
            QTabBar::tab:selected {{ background: rgba(52,211,153,0.25); color: #ecfdf5; }}
            QToolBar {{ background: transparent; border: 0; spacing: 4px; }}
            QToolBar QToolButton {{
                background: rgba(51,65,85,0.8); color: #e2e8f0;
                border-radius: 6px; padding: 4px 8px;
            }}
            QPushButton#primary {{
                background: #059669; color: white; border: 0; border-radius: 8px;
                padding: 6px 12px; font-weight: 700;
            }}
            QPushButton#soft {{
                background: rgba(51,65,85,0.9); color: #e2e8f0;
                border: 1px solid #475569; border-radius: 8px; padding: 5px 10px;
            }}
            QPushButton#danger {{
                background: rgba(127,29,29,0.85); color: #fecaca;
                border: 0; border-radius: 8px; padding: 5px 10px;
            }}
            QLabel#title {{ color: {C.get('accent', '#34d399')}; font-size: 16px; font-weight: 800; }}
            QLabel#section {{ color: #94a3b8; font-size: 11px; font-weight: 700; }}
            QLabel#muted {{ color: #94a3b8; font-size: 11px; }}
            """
        )

    # ---- refresh ----
    def refresh_sidebar(self) -> None:
        self.list_nb.blockSignals(True)
        self.list_nb.clear()
        all_item = QListWidgetItem("📚 全部笔记")
        all_item.setData(Qt.ItemDataRole.UserRole, "__all__")
        self.list_nb.addItem(all_item)
        for nb in self.store.list_notebooks():
            it = QListWidgetItem(f"📓 {nb.get('name')}")
            it.setData(Qt.ItemDataRole.UserRole, nb.get("id"))
            self.list_nb.addItem(it)
        # select current
        for i in range(self.list_nb.count()):
            if self.list_nb.item(i).data(Qt.ItemDataRole.UserRole) == self._current_notebook_id:
                self.list_nb.setCurrentRow(i)
                break
        else:
            self.list_nb.setCurrentRow(0)
        self.list_nb.blockSignals(False)

        self.list_tags.blockSignals(True)
        self.list_tags.clear()
        none = QListWidgetItem("（全部标签）")
        none.setData(Qt.ItemDataRole.UserRole, None)
        self.list_tags.addItem(none)
        for t in self.store.all_tags():
            it = QListWidgetItem(f"#{t}")
            it.setData(Qt.ItemDataRole.UserRole, t)
            self.list_tags.addItem(it)
        self.list_tags.setCurrentRow(0)
        self.list_tags.blockSignals(False)

        self.cmb_move.blockSignals(True)
        self.cmb_move.clear()
        for nb in self.store.list_notebooks():
            self.cmb_move.addItem(str(nb.get("name")), nb.get("id"))
        self.cmb_move.blockSignals(False)

    def refresh_note_list(self, *, keep_selection: bool = True) -> None:
        cur = self._current_note_id if keep_selection else None
        nb = None if self._current_notebook_id in ("__all__", None) else self._current_notebook_id
        notes = self.store.list_notes(
            notebook_id=nb,
            tag=self._current_tag,
            query=self.txt_search.text(),
        )
        self.list_notes.blockSignals(True)
        self.list_notes.clear()
        select_row = 0
        list_w = max(120, self.list_notes.viewport().width() - 16)
        for i, n in enumerate(notes):
            pin = "📌 " if n.get("pinned") else ""
            tags = " ".join(f"#{t}" for t in (n.get("tags") or [])[:4])
            title = str(n.get("title") or "无标题")
            snip = str(n.get("snippet") or "").strip()
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, n.get("id"))
            # Custom wrap-friendly widget (avoids mid-list ellipsis)
            cell = QWidget()
            cell.setObjectName("noteCell")
            cell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            cell.setProperty("selected", "false")
            vl = QVBoxLayout(cell)
            vl.setContentsMargins(10, 8, 10, 8)
            vl.setSpacing(3)
            lbl_title = QLabel(f"{pin}{title}")
            lbl_title.setWordWrap(True)
            lbl_title.setStyleSheet("font-weight: 700; color: #f1f5f9; background: transparent;")
            vl.addWidget(lbl_title)
            if snip:
                lbl_snip = QLabel(snip)
                lbl_snip.setWordWrap(True)
                lbl_snip.setStyleSheet("color: #94a3b8; font-size: 11px; background: transparent;")
                vl.addWidget(lbl_snip)
            if tags:
                lbl_tags = QLabel(tags)
                lbl_tags.setWordWrap(True)
                lbl_tags.setStyleSheet("color: #34d399; font-size: 11px; background: transparent;")
                vl.addWidget(lbl_tags)
            cell.setMinimumWidth(list_w)
            cell.adjustSize()
            hint_h = max(52, cell.sizeHint().height())
            it.setSizeHint(QSize(list_w, hint_h))
            self.list_notes.addItem(it)
            self.list_notes.setItemWidget(it, cell)
            if cur and n.get("id") == cur:
                select_row = i
        if self.list_notes.count():
            self.list_notes.setCurrentRow(select_row)
            self._sync_note_cell_selection()
        self.list_notes.blockSignals(False)

    def _refresh_attachments(self, meta: dict | None) -> None:
        self.list_att.clear()
        if not meta:
            return
        for a in meta.get("attachments") or []:
            size = int(a.get("size") or 0)
            label = f"{a.get('name')}  ({size // 1024} KB)" if size else str(a.get("name"))
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, a)
            self.list_att.addItem(it)

    # ---- selection handlers ----
    def _on_nb_selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None:
            return
        self._flush_save()
        self._current_notebook_id = str(cur.data(Qt.ItemDataRole.UserRole) or "__all__")
        self.refresh_note_list()

    def _on_tag_selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None:
            return
        self._flush_save()
        self._current_tag = cur.data(Qt.ItemDataRole.UserRole)
        self.refresh_note_list()

    def _on_search(self, _text: str = "") -> None:
        self.refresh_note_list()

    def _on_note_selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        self._sync_note_cell_selection()
        if cur is None:
            return
        nid = cur.data(Qt.ItemDataRole.UserRole)
        if nid and nid != self._current_note_id:
            self._flush_save()
            self._open_note(str(nid))

    def _sync_note_cell_selection(self) -> None:
        """Paint selection on the cell widget — no translucent item overlay/shadow."""
        cur = self.list_notes.currentItem()
        for i in range(self.list_notes.count()):
            it = self.list_notes.item(i)
            cell = self.list_notes.itemWidget(it) if it else None
            if cell is None:
                continue
            on = it is cur
            cell.setProperty("selected", "true" if on else "false")
            cell.style().unpolish(cell)
            cell.style().polish(cell)
            cell.update()

    def _open_note(self, note_id: str) -> None:
        meta, body = self.store.get_note(note_id)
        if meta is None:
            return
        self._loading = True
        self._current_note_id = note_id
        self.txt_title.setText(str(meta.get("title") or ""))
        self.txt_tags.setText(", ".join(str(t) for t in (meta.get("tags") or [])))
        # notebook combo
        nb_id = meta.get("notebook_id")
        idx = self.cmb_move.findData(nb_id)
        self.cmb_move.blockSignals(True)
        if idx >= 0:
            self.cmb_move.setCurrentIndex(idx)
        self.cmb_move.blockSignals(False)
        self.rich.setHtml(str(body.get("html") or ""))
        self.md_edit.setPlainText(str(body.get("markdown") or ""))
        self._update_md_preview()
        tab = str(body.get("active_tab") or "rich")
        self.tabs.blockSignals(True)
        self.tabs.setCurrentIndex(0 if tab != "markdown" else 1)
        self.tabs.blockSignals(False)
        self._refresh_attachments(meta)
        self._loading = False
        self._dirty = False
        self.lbl_status.setText(f"已打开 · {meta.get('updated') or ''}")

    # ---- mutations ----
    def _mark_dirty(self, *_args) -> None:
        if self._loading:
            return
        self._dirty = True
        self.lbl_status.setText("未保存…")
        self._save_timer.start(600)

    def _on_md_changed(self) -> None:
        self._mark_dirty()
        self._update_md_preview()

    def _update_md_preview(self) -> None:
        self.md_preview.setHtml(_markdown_to_html_simple(self.md_edit.toPlainText()))

    def _flush_save(self) -> None:
        if not self._current_note_id or self._loading:
            return
        if not self._dirty and not self._save_timer.isActive():
            # still allow forced save
            pass
        tags = [t.strip() for t in self.txt_tags.text().split(",") if t.strip()]
        active = "markdown" if self.tabs.currentIndex() == 1 else "rich"
        html = self.rich.toHtml()
        md = self.md_edit.toPlainText()
        self.store.update_note(
            self._current_note_id,
            title=self.txt_title.text(),
            html=html,
            markdown=md,
            active_tab=active,
            tags=tags,
        )
        self._dirty = False
        self.lbl_status.setText("已保存")
        self.refresh_note_list(keep_selection=True)
        self.refresh_sidebar()

    def _new_note(self) -> None:
        self._flush_save()
        nb = self._current_notebook_id if self._current_notebook_id not in ("__all__", None) else "nb_default"
        meta = self.store.create_note(notebook_id=str(nb), title="无标题笔记")
        self.refresh_note_list(keep_selection=False)
        self._open_note(str(meta["id"]))
        # select in list
        for i in range(self.list_notes.count()):
            if self.list_notes.item(i).data(Qt.ItemDataRole.UserRole) == meta["id"]:
                self.list_notes.setCurrentRow(i)
                break
        self.txt_title.setFocus()
        self.txt_title.selectAll()

    def _delete_note(self) -> None:
        if not self._current_note_id:
            return
        if QMessageBox.question(self, "删除笔记", "确定永久删除当前笔记？") != QMessageBox.StandardButton.Yes:
            return
        nid = self._current_note_id
        self.store.delete_note(nid, permanent=True)
        self._current_note_id = None
        self.refresh_note_list(keep_selection=False)
        self.refresh_sidebar()
        if self.list_notes.count():
            self.list_notes.setCurrentRow(0)
            nid2 = self.list_notes.item(0).data(Qt.ItemDataRole.UserRole)
            if nid2:
                self._open_note(str(nid2))
        else:
            self._loading = True
            self.txt_title.clear()
            self.txt_tags.clear()
            self.rich.clear()
            self.md_edit.clear()
            self.list_att.clear()
            self._loading = False

    def _toggle_pin(self) -> None:
        if not self._current_note_id:
            return
        self._flush_save()
        pinned = self.store.toggle_pin(self._current_note_id)
        self.lbl_status.setText("已置顶" if pinned else "已取消置顶")
        self.refresh_note_list()

    def _new_notebook(self) -> None:
        name, ok = QInputDialog.getText(self, "新建笔记本", "名称：")
        if not ok:
            return
        nb = self.store.create_notebook(name)
        self._current_notebook_id = str(nb["id"])
        self.refresh_sidebar()
        self.refresh_note_list()

    def _rename_notebook(self) -> None:
        nid = self._current_notebook_id
        if nid in ("__all__", None, "nb_default"):
            # allow rename default
            if nid == "__all__":
                QMessageBox.information(self, "提示", "请先选中一个笔记本")
                return
        meta = next((n for n in self.store.list_notebooks() if n.get("id") == nid), None)
        if not meta:
            return
        name, ok = QInputDialog.getText(self, "重命名笔记本", "名称：", text=str(meta.get("name") or ""))
        if not ok:
            return
        self.store.rename_notebook(str(nid), name)
        self.refresh_sidebar()

    def _delete_notebook(self) -> None:
        nid = self._current_notebook_id
        if nid in ("__all__", None):
            QMessageBox.information(self, "提示", "请先选中一个笔记本")
            return
        if nid == "nb_default":
            QMessageBox.information(self, "提示", "默认笔记本不能删除")
            return
        if QMessageBox.question(self, "删除笔记本", "删除后笔记会移到默认笔记本，确定？") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_notebook(str(nid))
        self._current_notebook_id = "__all__"
        self.refresh_sidebar()
        self.refresh_note_list()

    def _on_move_notebook(self, _idx: int = 0) -> None:
        if self._loading or not self._current_note_id:
            return
        nb_id = self.cmb_move.currentData()
        if not nb_id:
            return
        self.store.update_note(self._current_note_id, notebook_id=str(nb_id))
        self._dirty = False
        self.refresh_note_list()

    def _on_tab_changed(self, index: int) -> None:
        if self._loading:
            return
        # Convert when switching
        if index == 1:  # to markdown
            if not self.md_edit.toPlainText().strip():
                self.md_edit.blockSignals(True)
                self.md_edit.setPlainText(_html_to_markdown_simple(self.rich.toHtml()))
                self.md_edit.blockSignals(False)
                self._update_md_preview()
        else:  # to rich
            if not self.rich.toPlainText().strip():
                self.rich.blockSignals(True)
                self.rich.setHtml(_markdown_to_html_simple(self.md_edit.toPlainText()))
                self.rich.blockSignals(False)
            else:
                # keep rich; optionally refresh from md if md was last edited
                pass
        self._mark_dirty()

    # ---- formatting ----
    def _fmt_bold(self) -> None:
        fmt = QTextCharFormat()
        weight = self.rich.fontWeight()
        fmt.setFontWeight(QFont.Weight.Normal if weight > 50 else QFont.Weight.Bold)
        self.rich.mergeCurrentCharFormat(fmt)

    def _fmt_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.rich.fontItalic())
        self.rich.mergeCurrentCharFormat(fmt)

    def _fmt_h2(self) -> None:
        cursor = self.rich.textCursor()
        fmt = cursor.blockFormat()
        fmt.setHeadingLevel(2)
        cursor.setBlockFormat(fmt)
        cf = QTextCharFormat()
        cf.setFontPointSize(16)
        cf.setFontWeight(QFont.Weight.Bold)
        cursor.mergeCharFormat(cf)
        self.rich.setTextCursor(cursor)

    def _fmt_list(self) -> None:
        cursor = self.rich.textCursor()
        cursor.createList(QTextListFormat.Style.ListDisc)

    def _fmt_font_size(self, _index: int = 0) -> None:
        pt = self.cmb_font_size.currentData()
        if not pt:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(float(pt))
        self.rich.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def _show_color_palette(self, for_highlight: bool) -> None:
        """Dropdown color board — only shown when clicking A / 高亮."""
        pop = QFrame(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        pop.setStyleSheet(
            "QFrame#colorPop {"
            "  background:#111827; border:1px solid #475569; border-radius:14px;"
            "}"
            "QLabel { color:#cbd5e1; font-weight:700; font-size:12px; background:transparent; }"
            "QPushButton#soft {"
            "  background:#1e293b; color:#e2e8f0; border:1px solid #334155;"
            "  border-radius:8px; padding:6px 10px;"
            "}"
        )
        pop.setObjectName("colorPop")
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        lay.addWidget(QLabel("背景高亮" if for_highlight else "文字颜色"))
        row = QHBoxLayout()
        row.setSpacing(10)
        colors = _HIGHLIGHT_COLORS if for_highlight else _TEXT_COLORS
        for name, hex_c in colors:
            b = QPushButton()
            b.setFixedSize(28, 28)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(name + ("（清除高亮）" if for_highlight and not hex_c else ""))
            bg = hex_c if hex_c else "#0f172a"
            # Clear-highlight: draw a slash look
            if for_highlight and not hex_c:
                b.setText("∅")
                b.setStyleSheet(
                    "QPushButton { background:#0f172a; color:#f87171; border:2px solid #64748b;"
                    " border-radius:14px; font-weight:800; }"
                    "QPushButton:hover { border-color:#38bdf8; }"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background:{bg}; border:2px solid #94a3b8; border-radius:14px; }}"
                    f"QPushButton:hover {{ border:3px solid #38bdf8; }}"
                )
            if for_highlight:
                b.clicked.connect(lambda _=False, c=hex_c, p=pop: (self._apply_highlight(c), p.close()))
            else:
                b.clicked.connect(lambda _=False, c=hex_c, p=pop: (self._apply_text_color(c), p.close()))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        more = QPushButton("更多颜色…", objectName="soft")
        more.clicked.connect(
            lambda: (
                self._pick_highlight_dialog() if for_highlight else self._pick_text_color_dialog(),
                pop.close(),
            )
        )
        lay.addWidget(more)
        pop.adjustSize()
        anchor = self.btn_highlight if for_highlight else self.btn_text_color
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        pop.move(pos)
        pop.show()
        pop.raise_()

    def _apply_text_color(self, hex_color: str) -> None:
        if not hex_color:
            return
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(hex_color))
        self.rich.mergeCurrentCharFormat(fmt)
        self.btn_text_color.setStyleSheet(
            f"QToolButton {{ font-weight: 900; color: {hex_color}; padding: 4px 10px; }}"
        )
        self._mark_dirty()

    def _apply_highlight(self, hex_color: str) -> None:
        fmt = QTextCharFormat()
        if hex_color:
            fmt.setBackground(QColor(hex_color))
            self.btn_highlight.setStyleSheet(
                f"QToolButton {{ background: {hex_color}; color: #0f172a; font-weight: 700; "
                f"border-radius: 6px; padding: 4px 8px; }}"
            )
        else:
            fmt.setBackground(QColor(Qt.GlobalColor.transparent))
            self.btn_highlight.setStyleSheet(
                "QToolButton { background: #1e293b; color: #e2e8f0; font-weight: 700; "
                "border-radius: 6px; padding: 4px 8px; }"
            )
        self.rich.mergeCurrentCharFormat(fmt)
        self._mark_dirty()

    def _pick_text_color_dialog(self) -> None:
        color = QColorDialog.getColor(self.rich.textColor(), self, "选择文字颜色")
        if color.isValid():
            self._apply_text_color(color.name())

    def _pick_highlight_dialog(self) -> None:
        color = QColorDialog.getColor(QColor("#fef08a"), self, "选择背景高亮色")
        if color.isValid():
            self._apply_highlight(color.name())

    def _insert_link_dialog(self) -> None:
        url, ok = QInputDialog.getText(self, "插入超链接", "网址（https://…）：")
        if not ok:
            return
        self._on_link_pasted(url or "")

    def _on_link_pasted(self, url: str) -> None:
        cleaned = _normalize_http_url(url)
        if not cleaned:
            QMessageBox.warning(self, "链接无效", "无法解析该网址，请检查后重试。")
            return
        label = _guess_link_label(cleaned)
        # Insert immediately with fallback label, then upgrade title async
        self._insert_hyperlink(cleaned, label or cleaned)
        self.lbl_status.setText("正在读取链接标题…")

        def work() -> None:
            title = fetch_link_title(cleaned)
            if title:

                def apply() -> None:
                    self._upgrade_link_label(cleaned, title)
                    self.lbl_status.setText(f"链接标题：{title}")
                    self._mark_dirty()

                QTimer.singleShot(0, apply)
            else:

                def done() -> None:
                    self.lbl_status.setText("已插入超链接（未能读取标题，可能需登录）")

                QTimer.singleShot(0, done)

        threading.Thread(target=work, daemon=True).start()
    def _insert_hyperlink(self, url: str, label: str) -> None:
        if self.tabs.currentIndex() == 1:
            # Markdown tab
            safe_label = (label or url).replace("]", "")
            self.md_edit.insertPlainText(f"[{safe_label}]({url})")
            self._mark_dirty()
            return
        cursor = self.rich.textCursor()
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(QColor("#38bdf8"))
        fmt.setFontUnderline(True)
        cursor.insertText(label or url, fmt)
        # reset format so following text isn't a link
        cursor.setCharFormat(QTextCharFormat())
        self.rich.setTextCursor(cursor)
        self._mark_dirty()

    def _upgrade_link_label(self, url: str, new_label: str) -> None:
        """Replace display text of an existing anchor with matching href."""
        safe_label = (new_label or url).replace("<", "").replace(">", "")
        if self.tabs.currentIndex() == 1:
            text = self.md_edit.toPlainText()
            pat = re.compile(rf"\[([^\]]*)\]\({re.escape(url)}\)")
            if pat.search(text):
                self.md_edit.blockSignals(True)
                self.md_edit.setPlainText(pat.sub(f"[{safe_label}]({url})", text, count=1))
                self.md_edit.blockSignals(False)
                self._update_md_preview()
            return
        # Rich: rewrite first matching <a href="url">…</a> in HTML
        html = self.rich.toHtml()
        pat = re.compile(
            rf'(<a\b[^>]*\bhref="{re.escape(url)}"[^>]*>)(.*?)(</a>)',
            re.I | re.S,
        )
        new_html, n = pat.subn(rf"\g<1>{safe_label}\g<3>", html, count=1)
        if n:
            pos = self.rich.textCursor().position()
            self.rich.blockSignals(True)
            self.rich.setHtml(new_html)
            self.rich.blockSignals(False)
            c = self.rich.textCursor()
            c.setPosition(min(pos, len(self.rich.toPlainText())))
            self.rich.setTextCursor(c)

    def _image_max_edge(self) -> int:
        try:
            return int(self.cmb_img_size.currentData() or 1280)
        except Exception:
            return 1280

    def _insert_image_file(self) -> None:
        if not self._current_note_id:
            self._new_note()
        path, _ = QFileDialog.getOpenFileName(
            self, "插入图片", "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            self._on_image_pasted(data, Path(path).name)
        except Exception as e:
            QMessageBox.warning(self, "插入失败", str(e))

    def _on_image_pasted(self, data: bytes, name: str) -> None:
        if not self._current_note_id:
            self._new_note()
        assert self._current_note_id
        try:
            before = len(data)
            edge = self._image_max_edge()
            if edge <= 0:
                # 原图：不缩放，但仍可经 store 路径保存（skip compress by huge edge + png keep)
                att = self.store.add_image_bytes(
                    self._current_note_id,
                    data,
                    name=name or "paste.png",
                    max_edge=0,
                    jpeg_quality=95,
                )
            else:
                att = self.store.add_image_bytes(
                    self._current_note_id,
                    data,
                    name=name or "paste.jpg",
                    max_edge=edge,
                    jpeg_quality=85,
                )
            ap = self.store.attachment_path(self._current_note_id, att)
            url = ap.resolve().as_uri()
            if self.tabs.currentIndex() == 0:
                cursor = self.rich.textCursor()
                fmt = QTextImageFormat()
                fmt.setName(url)
                # Initial display width from compress setting (or natural, capped)
                try:
                    pix = QPixmap(str(ap))
                    nat_w = pix.width() if not pix.isNull() else 800
                    nat_h = pix.height() if not pix.isNull() else 600
                except Exception:
                    nat_w, nat_h = 800, 600
                disp = edge if edge > 0 else min(nat_w, 1280)
                disp = max(48, min(disp, nat_w if nat_w > 0 else disp))
                aspect = (nat_w / nat_h) if nat_h else 1.0
                fmt.setWidth(float(disp))
                fmt.setHeight(float(disp / max(0.05, aspect)))
                cursor.insertImage(fmt)
            else:
                self.md_edit.insertPlainText(f"\n![]({url})\n")
            meta, _ = self.store.get_note(self._current_note_id)
            self._refresh_attachments(meta)
            self._mark_dirty()
            after = int(att.get("size") or 0)
            saved = max(0, before - after)
            self.lbl_status.setText(
                f"已插入图片：{att.get('name')}  "
                f"({after // 1024} KB" + (f"，已压缩约 {saved // 1024} KB" if saved > 1024 else "") + ")"
            )
        except Exception as e:
            QMessageBox.warning(self, "图片失败", str(e))

    def _add_attachment(self) -> None:
        if not self._current_note_id:
            self._new_note()
        path, _ = QFileDialog.getOpenFileName(self, "添加附件", "", "All Files (*.*)")
        if not path:
            return
        try:
            before = Path(path).stat().st_size
            edge = self._image_max_edge()
            att = self.store.add_attachment(
                self._current_note_id,
                path,
                compress_images=(edge > 0),
                max_edge=edge if edge > 0 else 0,
                jpeg_quality=85,
            )
            meta, _ = self.store.get_note(self._current_note_id)
            self._refresh_attachments(meta)
            after = int(att.get("size") or 0)
            saved = max(0, before - after)
            msg = f"已添加附件：{att.get('name')}  ({after // 1024} KB"
            if saved > 1024:
                msg += f"，已压缩约 {saved // 1024} KB"
            self.lbl_status.setText(msg + ")")
        except Exception as e:
            QMessageBox.warning(self, "附件失败", str(e))

    def _open_attachment(self, item: QListWidgetItem) -> None:
        self._open_att_data(item.data(Qt.ItemDataRole.UserRole))

    def _open_selected_attachment(self) -> None:
        item = self.list_att.currentItem()
        if item:
            self._open_att_data(item.data(Qt.ItemDataRole.UserRole))

    def _open_att_data(self, att: Any) -> None:
        if not att or not self._current_note_id:
            return
        path = self.store.attachment_path(self._current_note_id, att)
        if not path.is_file():
            QMessageBox.warning(self, "附件", "文件不存在")
            return
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _remove_attachment(self) -> None:
        item = self.list_att.currentItem()
        if not item or not self._current_note_id:
            return
        att = item.data(Qt.ItemDataRole.UserRole) or {}
        self.store.remove_attachment(self._current_note_id, str(att.get("id") or ""))
        meta, _ = self.store.get_note(self._current_note_id)
        self._refresh_attachments(meta)

    # ---- cloud sync ----
    def _sync(self, *, push: bool) -> None:
        if self._sync_busy:
            return
        self._flush_save()
        cfg = dict(self.get_gdrive_cfg() or {})
        if not cfg.get("client_secrets_path") and not cfg.get("enabled"):
            # still try — token may exist from screenshot connect
            pass
        self._sync_busy = True
        self.lbl_status.setText("同步中…")

        def work() -> None:
            msg = ""
            try:
                from notebook_sync import NotebookSync

                sync = NotebookSync(self.store, cfg)
                if push:
                    msg = sync.push(on_status=lambda m: QTimer.singleShot(0, lambda: self.lbl_status.setText(m)))
                else:
                    msg = sync.pull(on_status=lambda m: QTimer.singleShot(0, lambda: self.lbl_status.setText(m)))
                if self.save_gdrive_cfg and sync.cfg.get("notebook_folder_id"):
                    cfg2 = dict(self.get_gdrive_cfg() or {})
                    cfg2["notebook_folder_id"] = sync.cfg["notebook_folder_id"]
                    try:
                        self.save_gdrive_cfg(cfg2)
                    except Exception:
                        pass
            except Exception as e:
                msg = f"同步失败：{e}"

            def done() -> None:
                self._sync_busy = False
                self.lbl_status.setText(msg)
                self.refresh_sidebar()
                self.refresh_note_list()
                if self._current_note_id:
                    self._open_note(self._current_note_id)

            QTimer.singleShot(0, done)

        threading.Thread(target=work, daemon=True).start()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._flush_save()
        except Exception:
            pass
        super().closeEvent(event)


def show_notebook_window(
    host,
    *,
    app_name: str = "DesktopToolkit",
) -> NotebookWindow:
    """Helper used by main.py / pet main.py."""
    win = getattr(host, "notebook_win", None)
    if win is None or not win.isVisible():
        store = NotebookStore(app_name=app_name)

        def get_cfg() -> dict:
            try:
                shot = (host.store.state.get("screenshot") or {}).get("gdrive") or {}
                return dict(shot)
            except Exception:
                return {}

        def save_cfg(cfg: dict) -> None:
            try:
                g = host.store.state.setdefault("screenshot", {}).setdefault("gdrive", {})
                if cfg.get("notebook_folder_id"):
                    g["notebook_folder_id"] = cfg["notebook_folder_id"]
                host.store.save_state()
            except Exception:
                pass

        win = NotebookWindow(
            store,
            app_name=app_name,
            get_gdrive_cfg=get_cfg,
            save_gdrive_cfg=save_cfg,
        )
        host.notebook_win = win
    win.show()
    win.raise_()
    win.activateWindow()
    return win
