"""Google Drive OAuth + upload for screenshots (desktop installed-app flow).

User provides OAuth Desktop client JSON from Google Cloud Console, then:
  1. Connect Google account (browser consent)
  2. Configure target folder ID (from Drive URL) or auto-create a folder by name
  3. Upload PNG/JPEG after capture
"""

from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable

# Files the app creates; enough for upload + create folder
SCOPES = "https://www.googleapis.com/auth/drive.file"
# Optional broader scope if user needs arbitrary existing folders
SCOPE_FULL = "https://www.googleapis.com/auth/drive"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


class GDriveError(RuntimeError):
    pass


def load_client_secrets(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        raise GDriveError(f"找不到客户端密钥文件: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    block = data.get("installed") or data.get("web") or data
    client_id = block.get("client_id")
    client_secret = block.get("client_secret")
    if not client_id or not client_secret:
        raise GDriveError("JSON 中缺少 client_id / client_secret（请用「桌面应用」类型 OAuth 客户端）")
    return {
        "client_id": str(client_id),
        "client_secret": str(client_secret),
        "auth_uri": str(block.get("auth_uri") or AUTH_URL),
        "token_uri": str(block.get("token_uri") or TOKEN_URL),
    }


def _http_json(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None, timeout: float = 60) -> Any:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise GDriveError(f"HTTP {e.code}: {err[:400]}") from e


class GoogleDriveClient:
    def __init__(self, cfg: dict) -> None:
        """cfg keys: client_secrets_path, token_path, folder_id, folder_name, use_full_scope."""
        self.cfg = cfg

    # ---- token storage ----
    def token_path(self) -> Path:
        p = self.cfg.get("token_path")
        if p:
            return Path(p)
        import os

        try:
            from storage import app_data_dir

            return app_data_dir() / "gdrive_token.json"
        except Exception:
            import os

            base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
            return Path(base) / "DesktopToolkit" / "gdrive_token.json"

    def load_token(self) -> dict | None:
        path = self.token_path()
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            try:
                from secret_store import unprotect

                raw = unprotect(raw)
            except Exception:
                pass
            return json.loads(raw)
        except Exception:
            return None

    def save_token(self, token: dict) -> None:
        path = self.token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(token, ensure_ascii=False, indent=2)
        try:
            from secret_store import protect

            raw = protect(raw)
        except Exception:
            pass
        path.write_text(raw, encoding="utf-8")

    def clear_token(self) -> None:
        path = self.token_path()
        if path.is_file():
            path.unlink()

    def is_connected(self) -> bool:
        t = self.load_token()
        return bool(t and (t.get("refresh_token") or t.get("access_token")))

    # ---- OAuth ----
    def authorize_interactive(
        self,
        *,
        on_status: Callable[[str], None] | None = None,
        timeout_sec: float = 180,
    ) -> dict:
        """Open browser, catch OAuth code on localhost, exchange for tokens."""
        status = on_status or (lambda m: None)
        secrets_path = self.cfg.get("client_secrets_path") or ""
        client = load_client_secrets(secrets_path)
        use_full = bool(self.cfg.get("use_full_scope"))
        scope = SCOPE_FULL if use_full else SCOPES
        state = secrets.token_urlsafe(16)
        code_holder: dict[str, str] = {}
        port_holder: dict[str, int] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path not in ("/", "/callback", "/oauth2callback"):
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("state", [""])[0] != state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"invalid state")
                    return
                if "error" in qs:
                    code_holder["error"] = qs["error"][0]
                else:
                    code_holder["code"] = qs.get("code", [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<html><body style='font-family:sans-serif;padding:40px'>"
                    "<h2>授权完成</h2><p>可以关闭此窗口，返回应用。</p>"
                    "</body></html>".encode("utf-8")
                )

            def log_message(self, fmt, *args):  # quiet
                return

        # Bind free port
        httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port_holder["port"] = httpd.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port_holder['port']}/oauth2callback"

        thr = threading.Thread(target=httpd.handle_request, daemon=True)
        thr.start()

        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        url = AUTH_URL + "?" + urllib.parse.urlencode(params)
        status("正在打开浏览器进行 Google 授权…")
        webbrowser.open(url)

        thr.join(timeout=timeout_sec)
        try:
            httpd.server_close()
        except Exception:
            pass

        if code_holder.get("error"):
            raise GDriveError(f"授权被拒绝: {code_holder['error']}")
        code = code_holder.get("code")
        if not code:
            raise GDriveError("授权超时或未收到授权码，请重试")

        status("正在换取访问令牌…")
        body = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")
        token = _http_json(
            "POST",
            client["token_uri"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        token["obtained_at"] = int(time.time())
        token["scope"] = scope
        self.save_token(token)
        status("Google 账号已连接")
        return token

    def access_token(self) -> str:
        token = self.load_token()
        if not token:
            raise GDriveError("尚未连接 Google 账号")
        exp = int(token.get("obtained_at") or 0) + int(token.get("expires_in") or 0) - 60
        if token.get("access_token") and time.time() < exp:
            return str(token["access_token"])
        refresh = token.get("refresh_token")
        if not refresh:
            raise GDriveError("令牌已过期且无 refresh_token，请重新连接 Google")
        secrets_path = self.cfg.get("client_secrets_path") or ""
        client = load_client_secrets(secrets_path)
        body = urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        fresh = _http_json(
            "POST",
            client["token_uri"],
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
        )
        token["access_token"] = fresh["access_token"]
        token["expires_in"] = fresh.get("expires_in", 3600)
        token["obtained_at"] = int(time.time())
        if fresh.get("refresh_token"):
            token["refresh_token"] = fresh["refresh_token"]
        self.save_token(token)
        return str(token["access_token"])

    # ---- Drive ops ----
    def ensure_folder(self) -> str:
        """Return folder_id: use configured id, or find/create by folder_name."""
        fid = (self.cfg.get("folder_id") or "").strip()
        if fid:
            return fid
        name = (self.cfg.get("folder_name") or "ToolkitShots").strip() or "ToolkitShots"
        # Search existing (drive.file may only see app-created)
        q = (
            f"name = '{name.replace(chr(39), chr(92) + chr(39))}' "
            f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        url = (
            f"{DRIVE_API}/files?"
            + urllib.parse.urlencode({"q": q, "spaces": "drive", "fields": "files(id,name)", "pageSize": "5"})
        )
        tok = self.access_token()
        data = _http_json("GET", url, headers={"Authorization": f"Bearer {tok}"})
        files = data.get("files") or []
        if files:
            fid = files[0]["id"]
            self.cfg["folder_id"] = fid
            return fid
        # Create
        meta = json.dumps({"name": name, "mimeType": "application/vnd.google-apps.folder"}).encode("utf-8")
        created = _http_json(
            "POST",
            f"{DRIVE_API}/files?fields=id,name",
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=meta,
        )
        fid = created["id"]
        self.cfg["folder_id"] = fid
        return fid

    def upload_file(
        self,
        local_path: str | Path,
        *,
        remote_name: str | None = None,
        mime: str | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> dict:
        status = on_status or (lambda m: None)
        path = Path(local_path)
        if not path.is_file():
            raise GDriveError(f"文件不存在: {path}")
        folder_id = self.ensure_folder()
        name = remote_name or path.name
        if not mime:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        status(f"上传到 Google Drive… ({name})")
        tok = self.access_token()
        metadata = {"name": name, "parents": [folder_id]}
        boundary = "parrot_boundary_" + secrets.token_hex(8)
        meta_part = json.dumps(metadata)
        file_bytes = path.read_bytes()
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{meta_part}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        url = f"{UPLOAD_API}/files?uploadType=multipart&fields=id,name,webViewLink"
        result = _http_json(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            data=body,
            timeout=120,
        )
        status(f"已上传: {result.get('name') or name}")
        return result
