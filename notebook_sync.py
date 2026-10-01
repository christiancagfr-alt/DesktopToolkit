"""Optional Google Drive sync for the notebook library.

Uses the same OAuth client as screenshot upload (drive.file scope).
Uploads index.json, note bodies, and attachments into a Drive folder
(default name: ToolkitNotebook). Download merges by updated timestamp.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from notebook_store import NotebookStore


StatusFn = Callable[[str], None]


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return 0.0


class NotebookSync:
    def __init__(self, store: NotebookStore, gdrive_cfg: dict) -> None:
        self.store = store
        # Use a dedicated folder name for notes; keep screenshot folder intact.
        self.cfg = dict(gdrive_cfg or {})
        if not self.cfg.get("folder_name") or self.cfg.get("folder_name") == "ToolkitShots":
            self.cfg["folder_name"] = "ToolkitNotebook"
        # Prefer separate folder_id key for notebook if present
        if self.cfg.get("notebook_folder_id"):
            self.cfg["folder_id"] = self.cfg["notebook_folder_id"]
        elif self.cfg.get("folder_name") == "ToolkitNotebook":
            # don't reuse screenshot folder_id
            self.cfg["folder_id"] = str(self.cfg.get("notebook_folder_id") or "")

    def _client(self):
        from gdrive_client import GoogleDriveClient

        return GoogleDriveClient(self.cfg)

    def push(self, *, on_status: StatusFn | None = None) -> str:
        status = on_status or (lambda m: None)
        client = self._client()
        status("打包本地笔记本…")
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "notebook_bundle.zip"
            with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                root = self.store.root
                for p in self.store.export_bundle_paths():
                    try:
                        arc = str(p.relative_to(root)).replace("\\", "/")
                    except ValueError:
                        arc = p.name
                    zf.write(p, arcname=arc)
            status("上传到 Google Drive…")
            result = client.upload_file(
                zpath,
                remote_name="notebook_bundle.zip",
                mime="application/zip",
                on_status=status,
            )
            # Remember notebook folder id separately if client created one
            if client.cfg.get("folder_id"):
                self.cfg["notebook_folder_id"] = client.cfg["folder_id"]
            link = result.get("webViewLink") or result.get("id") or ""
            return f"已上传笔记本备份{('：' + link) if link else ''}"

    def pull(self, *, on_status: StatusFn | None = None) -> str:
        """Download latest notebook_bundle.zip and merge newer notes into local store."""
        status = on_status or (lambda m: None)
        client = self._client()
        from gdrive_client import DRIVE_API, _http_json

        folder_id = client.ensure_folder()
        if client.cfg.get("folder_id"):
            self.cfg["notebook_folder_id"] = client.cfg["folder_id"]
        tok = client.access_token()
        q = (
            f"name = 'notebook_bundle.zip' and '{folder_id}' in parents "
            f"and trashed = false"
        )
        import urllib.parse

        url = (
            f"{DRIVE_API}/files?"
            + urllib.parse.urlencode(
                {
                    "q": q,
                    "spaces": "drive",
                    "fields": "files(id,name,modifiedTime)",
                    "pageSize": "5",
                    "orderBy": "modifiedTime desc",
                }
            )
        )
        data = _http_json("GET", url, headers={"Authorization": f"Bearer {tok}"})
        files = data.get("files") or []
        if not files:
            return "云端还没有笔记本备份，请先「上传同步」"
        file_id = files[0]["id"]
        status("下载云端备份…")
        import urllib.request

        req = urllib.request.Request(
            f"{DRIVE_API}/files/{file_id}?alt=media",
            headers={"Authorization": f"Bearer {tok}"},
        )
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "notebook_bundle.zip"
            with urllib.request.urlopen(req, timeout=120) as resp:
                zpath.write_bytes(resp.read())
            extract = Path(td) / "extracted"
            extract.mkdir()
            from zip_safe import safe_extractall

            with zipfile.ZipFile(zpath, "r") as zf:
                safe_extractall(zf, extract)
            status("合并到本地…")
            merged = self._merge_from_dir(extract)
        return f"已从云端合并：更新 {merged} 篇笔记"

    def _merge_from_dir(self, remote_root: Path) -> int:
        remote_index_path = remote_root / "index.json"
        if not remote_index_path.is_file():
            return 0
        remote_idx = json.loads(remote_index_path.read_text(encoding="utf-8"))
        local_idx = self.store._read_index()
        local_notes = {n["id"]: n for n in (local_idx.get("notes") or []) if n.get("id")}
        remote_notes = list(remote_idx.get("notes") or [])
        # merge notebooks by id
        local_nbs = {n["id"]: n for n in (local_idx.get("notebooks") or []) if n.get("id")}
        for nb in remote_idx.get("notebooks") or []:
            bid = nb.get("id")
            if not bid:
                continue
            if bid not in local_nbs:
                local_nbs[bid] = nb
            elif _parse_ts(nb.get("updated") or "") > _parse_ts(local_nbs[bid].get("updated") or ""):
                local_nbs[bid] = nb
        updated = 0
        for rn in remote_notes:
            rid = rn.get("id")
            if not rid:
                continue
            ln = local_notes.get(rid)
            remote_body_path = remote_root / "notes" / f"{rid}.json"
            if ln is None or _parse_ts(rn.get("updated") or "") > _parse_ts(ln.get("updated") or ""):
                local_notes[rid] = rn
                if remote_body_path.is_file():
                    dest = self.store._note_body_path(rid)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(remote_body_path.read_bytes())
                # attachments
                ratt = remote_root / "attachments" / rid
                if ratt.is_dir():
                    dest_a = self.store.attach_dir / rid
                    dest_a.mkdir(parents=True, exist_ok=True)
                    for f in ratt.iterdir():
                        if f.is_file():
                            (dest_a / f.name).write_bytes(f.read_bytes())
                updated += 1
        local_idx["notebooks"] = list(local_nbs.values())
        local_idx["notes"] = list(local_notes.values())
        self.store._write_index(local_idx)
        return updated
