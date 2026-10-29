"""Local Evernote-style notebook library (separate from sticky notes).

Layout under app data:
  notebook/
    index.json
    notes/<note_id>.json
    attachments/<note_id>/<file_id>_<safe_name>
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


_LOCK = threading.RLock()


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}" if prefix else uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    name = (name or "file").strip() or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:120]


def default_root(app_name: str = "DesktopToolkit") -> Path:
    try:
        from storage import app_data_dir

        return app_data_dir(app_name) / "notebook"
    except Exception:
        import os

        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name / "notebook"


class NotebookStore:
    """CRUD for notebooks, notes, tags, attachments. Sticky notes are unrelated."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        app_name: str = "DesktopToolkit",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.root = Path(root) if root else default_root(app_name)
        self.notes_dir = self.root / "notes"
        self.attach_dir = self.root / "attachments"
        self.index_path = self.root / "index.json"
        self.on_change = on_change
        self._ensure_layout()

    # ---- filesystem ----
    def _ensure_layout(self) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.attach_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.is_file():
            idx = self._blank_index()
            self._write_index(idx)
            self._seed_welcome(idx)
        else:
            idx = self._read_index()
            if not idx.get("notebooks"):
                idx["notebooks"] = [self._default_notebook()]
                self._write_index(idx)
            # only seed welcome when library is empty
            if not idx.get("notes"):
                self._seed_welcome(idx)

    def _blank_index(self) -> dict[str, Any]:
        nb = self._default_notebook()
        return {"version": 1, "notebooks": [nb], "notes": [], "updated": _now()}

    def _default_notebook(self) -> dict[str, Any]:
        return {
            "id": "nb_default",
            "name": "默认笔记本",
            "created": _now(),
            "updated": _now(),
        }

    def _seed_welcome(self, idx: dict[str, Any]) -> None:
        nid = "welcome"
        meta = {
            "id": nid,
            "notebook_id": "nb_default",
            "title": "欢迎使用笔记本",
            "tags": ["入门"],
            "pinned": True,
            "created": _now(),
            "updated": _now(),
            "snippet": "这是独立的笔记本功能，与桌面便签互不影响。支持笔记本分类、标签、搜索、置顶、附件与可选云同步。",
            "attachments": [],
        }
        body = {
            "id": nid,
            "format": "dual",
            "html": (
                "<h2>欢迎使用笔记本</h2>"
                "<p>这是<strong>独立的笔记本</strong>功能，与桌面「便签」互不影响。</p>"
                "<ul><li>左侧切换笔记本 / 标签</li>"
                "<li>中间搜索、置顶</li>"
                "<li>右侧富文本或 Markdown</li>"
                "<li>可粘贴截图、添加附件</li>"
                "<li>设置里可开启 Google Drive 云同步（可选）</li></ul>"
            ),
            "markdown": (
                "## 欢迎使用笔记本\n\n"
                "这是**独立的笔记本**功能，与桌面「便签」互不影响。\n\n"
                "- 左侧切换笔记本 / 标签\n"
                "- 中间搜索、置顶\n"
                "- 右侧富文本或 Markdown\n"
                "- 可粘贴截图、添加附件\n"
                "- 设置里可开启 Google Drive 云同步（可选）\n"
            ),
            "active_tab": "rich",
            "updated": _now(),
        }
        self._write_note_body(nid, body)
        idx.setdefault("notes", []).insert(0, meta)
        self._write_index(idx)

    def _read_index(self) -> dict[str, Any]:
        with _LOCK:
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                return self._blank_index()

    def _write_index(self, idx: dict[str, Any]) -> None:
        with _LOCK:
            idx["updated"] = _now()
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.index_path)
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _note_body_path(self, note_id: str) -> Path:
        note_id = self.validate_object_id(note_id, "笔记 ID")
        return self.notes_dir / f"{note_id}.json"

    @staticmethod
    def validate_object_id(value: str, label: str = "ID") -> str:
        """Return a safe application object ID or reject cloud-controlled paths."""
        text = str(value or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", text):
            raise ValueError(f"{label} 格式无效")
        return text

    def _contained_path(self, base: Path, relative: str) -> Path:
        """Resolve an untrusted relative path and keep it below *base*."""
        base = base.resolve()
        raw = str(relative or "").replace("\\", "/")
        if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("附件路径无效")
        target = (base / Path(*raw.split("/"))).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ValueError("附件路径越过笔记目录") from exc
        return target

    def _write_note_body(self, note_id: str, body: dict[str, Any]) -> None:
        with _LOCK:
            path = self._note_body_path(note_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _read_note_body(self, note_id: str) -> dict[str, Any]:
        path = self._note_body_path(note_id)
        if not path.is_file():
            return {
                "id": note_id,
                "format": "dual",
                "html": "",
                "markdown": "",
                "active_tab": "rich",
                "updated": _now(),
            }
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "id": note_id,
                "format": "dual",
                "html": "",
                "markdown": "",
                "active_tab": "rich",
                "updated": _now(),
            }

    # ---- notebooks ----
    def list_notebooks(self) -> list[dict]:
        return list(self._read_index().get("notebooks") or [])

    def create_notebook(self, name: str) -> dict:
        name = (name or "").strip()[:80] or "未命名笔记本"
        idx = self._read_index()
        nb = {"id": _uid("nb_"), "name": name, "created": _now(), "updated": _now()}
        idx.setdefault("notebooks", []).append(nb)
        self._write_index(idx)
        return nb

    def rename_notebook(self, notebook_id: str, name: str) -> str:
        name = (name or "").strip()[:80]
        if not name:
            return "名称不能为空"
        idx = self._read_index()
        for nb in idx.get("notebooks") or []:
            if nb.get("id") == notebook_id:
                nb["name"] = name
                nb["updated"] = _now()
                self._write_index(idx)
                return "已重命名"
        return "未找到笔记本"

    def delete_notebook(self, notebook_id: str, *, move_to: str | None = None) -> str:
        if notebook_id == "nb_default":
            return "默认笔记本不能删除"
        idx = self._read_index()
        nbs = idx.get("notebooks") or []
        if not any(n.get("id") == notebook_id for n in nbs):
            return "未找到笔记本"
        target = move_to or "nb_default"
        if not any(n.get("id") == target for n in nbs):
            target = "nb_default"
        for note in idx.get("notes") or []:
            if note.get("notebook_id") == notebook_id:
                note["notebook_id"] = target
                note["updated"] = _now()
        idx["notebooks"] = [n for n in nbs if n.get("id") != notebook_id]
        self._write_index(idx)
        return "已删除笔记本（笔记已移到默认笔记本）"

    # ---- notes ----
    def list_notes(
        self,
        *,
        notebook_id: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        include_trashed: bool = False,
    ) -> list[dict]:
        notes = list(self._read_index().get("notes") or [])
        if not include_trashed:
            notes = [n for n in notes if not n.get("trashed")]
        if notebook_id and notebook_id not in ("__all__", ""):
            notes = [n for n in notes if n.get("notebook_id") == notebook_id]
        if tag:
            tag_l = tag.lower()
            notes = [n for n in notes if tag_l in [str(t).lower() for t in (n.get("tags") or [])]]
        q = (query or "").strip().lower()
        if q:
            filtered: list[dict] = []
            for n in notes:
                hay = " ".join(
                    [
                        str(n.get("title") or ""),
                        str(n.get("snippet") or ""),
                        " ".join(str(t) for t in (n.get("tags") or [])),
                    ]
                ).lower()
                if q in hay:
                    filtered.append(n)
                    continue
                # deeper body search
                body = self._read_note_body(str(n.get("id") or ""))
                blob = f"{body.get('html') or ''} {body.get('markdown') or ''}".lower()
                # strip tags roughly
                blob = re.sub(r"<[^>]+>", " ", blob)
                if q in blob:
                    filtered.append(n)
            notes = filtered
        notes.sort(
            key=lambda n: (
                0 if n.get("pinned") else 1,
                str(n.get("updated") or ""),
            ),
            reverse=False,
        )
        # pinned first, then updated desc — fix sort
        notes.sort(key=lambda n: str(n.get("updated") or ""), reverse=True)
        notes.sort(key=lambda n: 0 if n.get("pinned") else 1)
        return notes

    def get_note_meta(self, note_id: str) -> dict | None:
        for n in self._read_index().get("notes") or []:
            if n.get("id") == note_id:
                return n
        return None

    def get_note(self, note_id: str) -> tuple[dict | None, dict]:
        meta = self.get_note_meta(note_id)
        body = self._read_note_body(note_id)
        return meta, body

    def create_note(
        self,
        *,
        notebook_id: str = "nb_default",
        title: str = "无标题笔记",
        html: str = "",
        markdown: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        idx = self._read_index()
        if not any(n.get("id") == notebook_id for n in (idx.get("notebooks") or [])):
            notebook_id = "nb_default"
        nid = _uid("n_")
        title = (title or "").strip()[:200] or "无标题笔记"
        tags = [t.strip()[:40] for t in (tags or []) if str(t).strip()]
        snippet = self._snippet_from(html, markdown)
        meta = {
            "id": nid,
            "notebook_id": notebook_id,
            "title": title,
            "tags": tags,
            "pinned": False,
            "created": _now(),
            "updated": _now(),
            "snippet": snippet,
            "attachments": [],
        }
        body = {
            "id": nid,
            "format": "dual",
            "html": html,
            "markdown": markdown,
            "active_tab": "rich",
            "updated": _now(),
        }
        self._write_note_body(nid, body)
        idx.setdefault("notes", []).insert(0, meta)
        self._write_index(idx)
        return meta

    def update_note(
        self,
        note_id: str,
        *,
        title: str | None = None,
        html: str | None = None,
        markdown: str | None = None,
        active_tab: str | None = None,
        tags: list[str] | None = None,
        notebook_id: str | None = None,
        pinned: bool | None = None,
    ) -> str:
        idx = self._read_index()
        meta = next((n for n in (idx.get("notes") or []) if n.get("id") == note_id), None)
        if meta is None:
            return "未找到笔记"
        body = self._read_note_body(note_id)
        if title is not None:
            meta["title"] = (title or "").strip()[:200] or "无标题笔记"
        if tags is not None:
            meta["tags"] = [t.strip()[:40] for t in tags if str(t).strip()]
        if notebook_id is not None:
            meta["notebook_id"] = notebook_id
        if pinned is not None:
            meta["pinned"] = bool(pinned)
        if html is not None:
            body["html"] = html
        if markdown is not None:
            body["markdown"] = markdown
        if active_tab is not None:
            body["active_tab"] = active_tab
        body["updated"] = _now()
        meta["updated"] = _now()
        meta["snippet"] = self._snippet_from(body.get("html") or "", body.get("markdown") or "")
        self._write_note_body(note_id, body)
        self._write_index(idx)
        return "已保存"

    def delete_note(self, note_id: str, *, permanent: bool = True) -> str:
        idx = self._read_index()
        notes = idx.get("notes") or []
        meta = next((n for n in notes if n.get("id") == note_id), None)
        if meta is None:
            return "未找到笔记"
        if permanent:
            idx["notes"] = [n for n in notes if n.get("id") != note_id]
            self._write_index(idx)
            try:
                self._note_body_path(note_id).unlink(missing_ok=True)
            except Exception:
                pass
            adir = self.attach_dir / note_id
            if adir.is_dir():
                shutil.rmtree(adir, ignore_errors=True)
            return "已永久删除"
        meta["trashed"] = True
        meta["updated"] = _now()
        self._write_index(idx)
        return "已移入回收站"

    def toggle_pin(self, note_id: str) -> bool:
        idx = self._read_index()
        for n in idx.get("notes") or []:
            if n.get("id") == note_id:
                n["pinned"] = not bool(n.get("pinned"))
                n["updated"] = _now()
                self._write_index(idx)
                return bool(n["pinned"])
        return False

    def all_tags(self) -> list[str]:
        tags: set[str] = set()
        for n in self._read_index().get("notes") or []:
            if n.get("trashed"):
                continue
            for t in n.get("tags") or []:
                s = str(t).strip()
                if s:
                    tags.add(s)
        return sorted(tags, key=lambda x: x.lower())

    @staticmethod
    def _snippet_from(html: str, markdown: str) -> str:
        text = markdown or re.sub(r"<[^>]+>", " ", html or "")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:160]

    # ---- attachments ----
    @staticmethod
    def compress_image_bytes(
        data: bytes,
        *,
        max_edge: int = 1280,
        jpeg_quality: int = 85,
        source_name: str = "image.jpg",
    ) -> tuple[bytes, str, str]:
        """Downscale + JPEG-compress image. Returns (bytes, filename, mime).

        Keeps GIF/animated as original if decode fails. Transparent PNG → RGB on white.
        """
        if max_edge == 0:
            # Explicit "keep original"
            name = _safe_name(source_name)
            suf = Path(name).suffix.lower()
            mime = "application/octet-stream"
            if suf in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                mime = f"image/{suf.lstrip('.').replace('jpg', 'jpeg')}"
            return data, name, mime
        try:
            from io import BytesIO

            from PIL import Image

            im = Image.open(BytesIO(data))
            im.load()
            # Normalize mode
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode == "P":
                    im = im.convert("RGBA")
                if im.mode in ("RGBA", "LA"):
                    alpha = im.split()[-1]
                    bg.paste(im.convert("RGBA"), mask=alpha)
                    im = bg
                else:
                    im = im.convert("RGB")
            elif im.mode != "RGB":
                im = im.convert("RGB")
            w, h = im.size
            edge = max(w, h)
            if edge > max_edge > 0:
                scale = max_edge / float(edge)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            out = BytesIO()
            im.save(out, format="JPEG", quality=int(jpeg_quality), optimize=True)
            raw = out.getvalue()
            # Only use compressed if actually smaller (or resized)
            if len(raw) < len(data) or edge > max_edge:
                base = Path(_safe_name(source_name)).stem or "image"
                return raw, f"{base}.jpg", "image/jpeg"
        except Exception:
            pass
        # Fallback: store original
        name = _safe_name(source_name)
        suf = Path(name).suffix.lower()
        mime = "application/octet-stream"
        if suf in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            mime = f"image/{suf.lstrip('.').replace('jpg', 'jpeg')}"
        return data, name, mime

    def add_attachment(
        self,
        note_id: str,
        src_path: str | Path,
        *,
        display_name: str | None = None,
        compress_images: bool = True,
        max_edge: int = 1280,
        jpeg_quality: int = 85,
    ) -> dict:
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(str(src))
        meta = self.get_note_meta(note_id)
        if meta is None:
            raise KeyError(note_id)
        aid = _uid("a_")
        name = _safe_name(display_name or src.name)
        dest_dir = self.attach_dir / note_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        suf = src.suffix.lower()
        is_image = suf in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
        mime = "application/octet-stream"
        if compress_images and is_image:
            raw, name, mime = self.compress_image_bytes(
                src.read_bytes(),
                max_edge=max_edge,
                jpeg_quality=jpeg_quality,
                source_name=name,
            )
            dest = dest_dir / f"{aid}_{name}"
            dest.write_bytes(raw)
        else:
            dest = dest_dir / f"{aid}_{name}"
            shutil.copy2(src, dest)
            if is_image:
                mime = f"image/{suf.lstrip('.').replace('jpg', 'jpeg')}"
            elif suf == ".pdf":
                mime = "application/pdf"
            elif suf in {".txt", ".md"}:
                mime = "text/plain"
        rel = f"attachments/{note_id}/{dest.name}"
        item = {
            "id": aid,
            "name": name,
            "rel": rel.replace("\\", "/"),
            "mime": mime,
            "size": dest.stat().st_size,
            "added": _now(),
        }
        idx = self._read_index()
        for n in idx.get("notes") or []:
            if n.get("id") == note_id:
                n.setdefault("attachments", []).append(item)
                n["updated"] = _now()
                break
        self._write_index(idx)
        return item

    def add_image_bytes(
        self,
        note_id: str,
        data: bytes,
        *,
        name: str = "paste.png",
        max_edge: int = 1280,
        jpeg_quality: int = 85,
    ) -> dict:
        raw, out_name, _mime = self.compress_image_bytes(
            data, max_edge=max_edge, jpeg_quality=jpeg_quality, source_name=name or "paste.jpg"
        )
        tmp = self.root / "_tmp_paste"
        tmp.mkdir(parents=True, exist_ok=True)
        path = tmp / _safe_name(out_name)
        path.write_bytes(raw)
        try:
            # Already compressed — skip second pass
            return self.add_attachment(
                note_id, path, display_name=out_name, compress_images=False
            )
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
    def attachment_path(self, note_id: str, att: dict) -> Path:
        note_id = self.validate_object_id(note_id, "笔记 ID")
        rel = str(att.get("rel") or "").replace("\\", "/")
        if rel:
            target = self._contained_path(self.root, rel)
            allowed = (self.attach_dir / note_id).resolve()
            try:
                target.relative_to(allowed)
            except ValueError as exc:
                raise ValueError("附件路径不属于当前笔记") from exc
            return target
        att_id = self.validate_object_id(str(att.get("id") or ""), "附件 ID")
        return self.attach_dir / note_id / f"{att_id}_{_safe_name(str(att.get('name') or 'file'))}"
    def remove_attachment(self, note_id: str, att_id: str) -> str:
        idx = self._read_index()
        for n in idx.get("notes") or []:
            if n.get("id") != note_id:
                continue
            kept = []
            removed = None
            for a in n.get("attachments") or []:
                if a.get("id") == att_id:
                    removed = a
                else:
                    kept.append(a)
            if removed is None:
                return "未找到附件"
            n["attachments"] = kept
            n["updated"] = _now()
            self._write_index(idx)
            try:
                self.attachment_path(note_id, removed).unlink(missing_ok=True)
            except Exception:
                pass
            return "已删除附件"
        return "未找到笔记"

    def export_bundle_paths(self) -> list[Path]:
        """All files that cloud sync should upload."""
        paths = [self.index_path]
        if self.notes_dir.is_dir():
            paths.extend(sorted(self.notes_dir.glob("*.json")))
        if self.attach_dir.is_dir():
            for p in self.attach_dir.rglob("*"):
                if p.is_file():
                    paths.append(p)
        return paths
