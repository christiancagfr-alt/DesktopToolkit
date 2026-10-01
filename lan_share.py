"""Password-protected LAN file sharing for family / office media transfer.

Uses only the Python standard library so it packages cleanly with PyInstaller.
One machine hosts a shared folder on a TCP port; others connect with the password
to list, download, and upload files over the local network (much faster than cloud
upload/download for large 自媒体 editing assets).
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


DEFAULT_PORT = 8765
CHUNK_SIZE = 1024 * 256  # 256 KiB — good for large media files


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of non-loopback IPv4 addresses on this machine."""
    found: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    # Also try the "connect to public DNS" trick to discover the primary NIC.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith("127.") and ip not in found:
            found.insert(0, ip)
    except OSError:
        pass
    if not found:
        found.append("127.0.0.1")
    return found


# PBKDF2 iterations — CodeQL requires a slow KDF for password hashing (not raw SHA-256).
_PBKDF2_ITERS = 200_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (salt_hex, digest_hex) using PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
        salt = salt_bytes.hex()
    else:
        try:
            salt_bytes = bytes.fromhex(salt)
        except ValueError:
            salt_bytes = salt.encode("utf-8")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt_bytes,
        _PBKDF2_ITERS,
    ).hex()
    return salt, digest


def verify_password(password: str, salt: str, digest: str) -> bool:
    try:
        _, check = hash_password(password, salt)
        return secrets.compare_digest(check, digest or "")
    except Exception:
        return False


def _safe_join(root: Path, rel: str) -> Path | None:
    """Resolve rel under root; reject path traversal."""
    root = root.resolve()
    # Use unquote (NOT unquote_plus): '+' is a valid filename char, not a space.
    cleaned = (rel or "").replace("\\", "/")
    try:
        cleaned = urllib.parse.unquote(cleaned)
        if "%" in cleaned:
            cleaned = urllib.parse.unquote(cleaned)
    except Exception:
        pass
    cleaned = cleaned.lstrip("/")
    # Disallow absolute / parent escapes
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return None
    target = root.joinpath(*parts).resolve() if parts else root
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def _query_path(qs: dict[str, list[str]]) -> str:
    """Extract and normalize ?path= from a query string map.

    Important: do NOT use unquote_plus — a real file named ``a+b.txt`` must
    stay as ``a+b.txt`` (``+`` is not a space in file paths).
    """
    raw = (qs.get("path") or [""])[0]
    if not raw:
        return ""
    try:
        # parse_qs already percent-decodes once; only re-unquote if still encoded
        once = urllib.parse.unquote(raw)
        if "%" in once:
            once = urllib.parse.unquote(once)
        return once.replace("\\", "/")
    except Exception:
        return str(raw).replace("\\", "/")


class _ShareHandler(BaseHTTPRequestHandler):
    # Set by LanShareServer before serving
    share_root: Path = Path(".")
    # Password is stored only as PBKDF2 salt+digest (never plaintext on the handler).
    password_salt: str = ""
    password_digest: str = ""
    token: str = ""
    log_callback: Callable[[str], None] | None = None
    # Auth brute-force guard: client IP -> list of failure timestamps
    _auth_failures: dict[str, list[float]] = {}
    _auth_lock = threading.Lock()
    _AUTH_WINDOW_SEC = 60.0
    _AUTH_MAX_FAILS = 8
    _AUTH_LOCKOUT_SEC = 120.0

    def log_message(self, fmt: str, *args) -> None:  # quieter default
        if self.log_callback:
            try:
                self.log_callback(fmt % args)
            except Exception:
                pass

    def _client_ip(self) -> str:
        try:
            return str(self.client_address[0] or "")
        except Exception:
            return ""

    def _auth_blocked(self) -> bool:
        ip = self._client_ip()
        if not ip:
            return False
        now = __import__("time").time()
        with _ShareHandler._auth_lock:
            stamps = [
                t
                for t in _ShareHandler._auth_failures.get(ip, [])
                if now - t <= _ShareHandler._AUTH_LOCKOUT_SEC
            ]
            _ShareHandler._auth_failures[ip] = stamps
            recent = [t for t in stamps if now - t <= _ShareHandler._AUTH_WINDOW_SEC]
            return len(recent) >= _ShareHandler._AUTH_MAX_FAILS

    def _auth_fail(self) -> None:
        ip = self._client_ip()
        if not ip:
            return
        now = __import__("time").time()
        with _ShareHandler._auth_lock:
            stamps = _ShareHandler._auth_failures.setdefault(ip, [])
            stamps.append(now)
            _ShareHandler._auth_failures[ip] = [
                t for t in stamps if now - t <= _ShareHandler._AUTH_LOCKOUT_SEC
            ]

    def _auth_ok_reset(self) -> None:
        ip = self._client_ip()
        if not ip:
            return
        with _ShareHandler._auth_lock:
            _ShareHandler._auth_failures.pop(ip, None)

    def _credential_ok(self, provided: str) -> bool:
        if not provided:
            return False
        # Session token is high-entropy random — constant-time compare is enough.
        if self.token and secrets.compare_digest(provided, self.token):
            return True
        if self.password_salt and self.password_digest:
            return verify_password(provided, self.password_salt, self.password_digest)
        return False

    def _auth_ok(self) -> bool:
        # Accept Authorization: Bearer <token|password> or X-Share-Password / X-Share-Token
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            if self._credential_ok(auth[7:].strip()):
                self._auth_ok_reset()
                return True
        for header in ("X-Share-Token", "X-Share-Password"):
            if self._credential_ok((self.headers.get(header) or "").strip()):
                self._auth_ok_reset()
                return True
        # Do not accept ?token= / ?password= — credentials in the URL land in
        # proxy/server access logs and browser history.
        self._auth_fail()
        return False

    def _require_auth(self) -> bool:
        """Return True if authorized. Sends 429/401 on failure."""
        if self._auth_blocked():
            self._send_error_json(429, "尝试次数过多，请稍后再试")
            return False
        if not self._auth_ok():
            self._send_error_json(401, "密码错误或未授权")
            return False
        return True

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _send_error_json(self, code: int, message: str) -> None:
        self._send_json(code, {"ok": False, "error": message})

    def do_OPTIONS(self) -> None:  # type: ignore[override]
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Share-Password, X-Share-Token")
        self.end_headers()

    def do_GET(self) -> None:  # type: ignore[override]
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        rel = _query_path(qs)

        if path in ("/api/ping", "/api/info"):
            self._send_json(
                200,
                {
                    "ok": True,
                    "name": socket.gethostname(),
                    "root_name": self.share_root.name or str(self.share_root),
                },
            )
            return

        if path == "/api/list":
            target = _safe_join(self.share_root, rel)
            if target is None or not target.exists():
                self._send_error_json(404, f"路径不存在：{rel or '/'}")
                return
            if not target.is_dir():
                self._send_error_json(400, "不是文件夹")
                return
            entries: list[dict[str, Any]] = []
            root_resolved = self.share_root.resolve()
            try:
                for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    try:
                        stat = child.stat()
                        try:
                            child_rel = child.resolve().relative_to(root_resolved).as_posix()
                        except ValueError:
                            child_rel = child.name
                        entries.append(
                            {
                                "name": child.name,
                                "path": child_rel,
                                "is_dir": child.is_dir(),
                                "size": 0 if child.is_dir() else int(stat.st_size),
                                "mtime": int(stat.st_mtime),
                            }
                        )
                    except OSError:
                        continue
            except OSError as exc:
                self._send_error_json(500, f"无法读取目录：{exc}")
                return
            self._send_json(200, {"ok": True, "path": rel.replace("\\", "/"), "entries": entries})
            return

        # Aliases: /api/download and /api/file
        if path in ("/api/download", "/api/file"):
            target = _safe_join(self.share_root, rel)
            if target is None:
                self._send_error_json(404, f"非法路径：{rel}")
                return
            if not target.exists():
                self._send_error_json(404, f"文件不存在：{rel}")
                return
            if not target.is_file():
                self._send_error_json(400, f"不是文件（可能是文件夹）：{rel}")
                return
            try:
                size = target.stat().st_size
            except OSError as exc:
                self._send_error_json(500, str(exc))
                return
            # Optional Range: bytes=start-  for resume / partial download
            range_header = self.headers.get("Range") or ""
            start = 0
            end = size - 1
            status = 200
            if range_header.lower().startswith("bytes=") and size > 0:
                try:
                    spec = range_header.split("=", 1)[1]
                    start_s, _, end_s = spec.partition("-")
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else (size - 1)
                    start = max(0, min(start, size - 1))
                    end = max(start, min(end, size - 1))
                    status = 206
                except ValueError:
                    start, end, status = 0, size - 1, 200
            length = end - start + 1 if size else 0
            mime, _ = mimetypes.guess_type(str(target))
            self.send_response(status)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            filename = urllib.parse.quote(target.name)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{filename}",
            )
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                with target.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except OSError:
                pass
            return

        if path == "/api/list_tree":
            # Recursive file list under path (for folder download / sync)
            target = _safe_join(self.share_root, rel)
            if target is None or not target.exists():
                self._send_error_json(404, f"路径不存在：{rel or '/'}")
                return
            if not target.is_dir():
                self._send_error_json(400, "不是文件夹")
                return
            files: list[dict[str, Any]] = []
            root_resolved = self.share_root.resolve()
            try:
                for child in target.rglob("*"):
                    if not child.is_file():
                        continue
                    try:
                        rel_file = child.resolve().relative_to(root_resolved).as_posix()
                        st = child.stat()
                        files.append(
                            {
                                "name": child.name,
                                "path": rel_file,
                                "is_dir": False,
                                "size": int(st.st_size),
                                "mtime": int(st.st_mtime),
                            }
                        )
                    except (OSError, ValueError):
                        continue
            except OSError as exc:
                self._send_error_json(500, f"无法枚举目录：{exc}")
                return
            self._send_json(200, {"ok": True, "path": rel.replace("\\", "/"), "files": files})
            return

        self._send_error_json(404, f"未知接口：{path}")

    def do_POST(self) -> None:  # type: ignore[override]
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        rel = _query_path(qs)
        length = int(self.headers.get("Content-Length") or 0)

        if path == "/api/mkdir":
            target = _safe_join(self.share_root, rel)
            if target is None:
                self._send_error_json(400, "非法路径")
                return
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._send_error_json(500, str(exc))
                return
            self._send_json(200, {"ok": True})
            return

        if path == "/api/upload":
            # path = destination directory (relative); filename from header or query
            dest_dir = _safe_join(self.share_root, rel)
            if dest_dir is None:
                self._send_error_json(400, "非法路径")
                return
            filename = (qs.get("name") or [""])[0] or self.headers.get("X-Filename") or "upload.bin"
            filename = Path(filename).name  # strip any path
            if not filename:
                self._send_error_json(400, "缺少文件名")
                return
            if not dest_dir.exists():
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    self._send_error_json(500, str(exc))
                    return
            if not dest_dir.is_dir():
                self._send_error_json(400, "目标不是文件夹")
                return
            out_path = dest_dir / filename
            # Extra safety: resolve and check still under root
            try:
                resolved = out_path.resolve()
                resolved.relative_to(self.share_root.resolve())
            except (OSError, ValueError):
                self._send_error_json(400, "非法文件名")
                return
            remaining = length
            try:
                with out_path.open("wb") as handle:
                    while remaining > 0:
                        chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        handle.write(chunk)
                        remaining -= len(chunk)
            except OSError as exc:
                self._send_error_json(500, f"写入失败：{exc}")
                return
            self._send_json(200, {"ok": True, "name": filename, "size": length})
            return

        self._send_error_json(404, "未知接口")


class LanShareServer:
    """Background threaded HTTP file share host."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.root: Path | None = None
        self.port: int = DEFAULT_PORT
        self.password: str = ""
        self.token: str = ""
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    def start(
        self,
        root: str | Path,
        password: str,
        port: int = DEFAULT_PORT,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        with self._lock:
            if self.running:
                return "分享服务已在运行，请先停止。"
            folder = Path(root).expanduser()
            if not folder.is_dir():
                return "分享目录不存在，请先选择有效文件夹。"
            pwd = (password or "").strip()
            if len(pwd) < 4:
                return "请设置至少 4 位访问密码。"
            try:
                port = int(port)
            except (TypeError, ValueError):
                port = DEFAULT_PORT
            if not (1024 <= port <= 65535):
                return "端口请使用 1024–65535。"

            token = secrets.token_urlsafe(16)
            pwd_salt, pwd_digest = hash_password(pwd)
            handler = type(
                "BoundShareHandler",
                (_ShareHandler,),
                {
                    "share_root": folder.resolve(),
                    "password_salt": pwd_salt,
                    "password_digest": pwd_digest,
                    "token": token,
                    "log_callback": staticmethod(log_callback) if log_callback else None,
                },
            )
            try:
                server = ThreadingHTTPServer(("0.0.0.0", port), handler)
            except OSError as exc:
                return f"无法绑定端口 {port}：{exc}"

            self._server = server
            self.root = folder.resolve()
            self.port = port
            self.password = pwd
            self.token = token

            def _run() -> None:
                try:
                    server.serve_forever(poll_interval=0.5)
                except Exception:
                    pass

            self._thread = threading.Thread(target=_run, name="LanShareServer", daemon=True)
            self._thread.start()

            ips = local_ipv4_addresses()
            tip = " / ".join(f"{ip}:{port}" for ip in ips[:3])
            return f"已开启局域网分享：{tip}（密码保护）"

    def stop(self) -> str:
        with self._lock:
            if not self._server:
                return "分享服务未在运行。"
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None
            self.root = None
            self.token = ""
            return "已停止局域网分享。"

    def status_text(self) -> str:
        if not self.running:
            return "未开启"
        ips = local_ipv4_addresses()
        root_name = self.root.name if self.root else "?"
        return f"运行中 · {root_name} · 端口 {self.port} · {', '.join(ips[:3])}"


class LanShareClient:
    """Simple HTTP client for connecting to another pet's share server."""

    def __init__(self) -> None:
        self.base_url = ""
        self.password = ""
        self.connected_name = ""
        self.remote_root_name = ""

    @property
    def connected(self) -> bool:
        return bool(self.base_url)

    def disconnect(self) -> None:
        self.base_url = ""
        self.password = ""
        self.connected_name = ""
        self.remote_root_name = ""

    def connect(self, host: str, port: int, password: str, timeout: float = 5.0) -> str:
        raw = (host or "").strip().strip("/")
        # Accept pasted URLs; extract hostname via urlparse (not substring strip)
        try:
            from urllib.parse import urlparse

            candidate = raw if "://" in raw else f"http://{raw}"
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").strip(".")
            if parsed.port and not port:
                port = parsed.port
        except Exception:
            host = raw.split("/")[0].split(":")[0]
        if not host:
            return "请填写主机 IP 或主机名。"
        pwd = (password or "").strip()
        if not pwd:
            return "请填写访问密码。"
        try:
            port = int(port)
        except (TypeError, ValueError):
            port = DEFAULT_PORT
        base = f"http://{host}:{port}"
        try:
            data = self._request_json(base, "/api/ping", password=pwd, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return "连接失败：密码错误或未授权。"
            return f"连接失败：HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return f"连接失败：无法访问主机（请确认对方已开启分享，且在同一局域网）。{exc.reason}"
        except Exception as exc:
            return f"连接失败：{exc}"
        if not data.get("ok"):
            return f"连接失败：{data.get('error') or '未知错误'}"
        self.base_url = base
        self.password = pwd
        self.connected_name = str(data.get("name") or host)
        self.remote_root_name = str(data.get("root_name") or "共享")
        return f"已连接 {self.connected_name}（共享：{self.remote_root_name}）"

    def list_dir(self, rel_path: str = "") -> tuple[str | None, list[dict[str, Any]]]:
        if not self.connected:
            return "尚未连接", []
        try:
            data = self._request_json(
                self.base_url,
                "/api/list",
                params={"path": rel_path or ""},
                password=self.password,
            )
        except Exception as exc:
            return str(exc), []
        if not data.get("ok"):
            return str(data.get("error") or "列出失败"), []
        return None, list(data.get("entries") or [])

    def download(
        self,
        rel_path: str,
        dest_file: str | Path,
        progress: Callable[[int, int], None] | None = None,
        *,
        resume: bool = True,
    ) -> str:
        if not self.connected:
            return "尚未连接"
        rel = (rel_path or "").replace("\\", "/").strip()
        if not rel or rel in (".", "/"):
            return "请指定要下载的文件路径"
        # Encode full path (including /) so proxies never mis-parse query
        query = urllib.parse.urlencode({"path": rel})
        url = f"{self.base_url}/api/download?{query}"
        headers = {"Authorization": f"Bearer {self.password}"}
        dest = Path(dest_file)
        existing = dest.stat().st_size if resume and dest.exists() else 0
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"
        try:
            try:
                import requests

                with requests.get(url, headers=headers, stream=True, timeout=120) as resp:
                    if resp.status_code not in (200, 206):
                        detail = self._http_error_detail(resp.status_code, resp.content, rel)
                        return detail
                    total = existing
                    cl = resp.headers.get("Content-Length")
                    if cl and str(cl).isdigit():
                        total = existing + int(cl) if resp.status_code == 206 else int(cl)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    mode = "ab" if resp.status_code == 206 and existing else "wb"
                    if mode == "wb":
                        existing = 0
                    written = existing if mode == "ab" else 0
                    with dest.open(mode) as handle:
                        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            written += len(chunk)
                            if progress:
                                progress(written, total)
            except ImportError:
                req = urllib.request.Request(url, method="GET")
                for k, v in headers.items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    total = int(resp.headers.get("Content-Length") or 0)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with dest.open("wb") as handle:
                        while True:
                            chunk = resp.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            handle.write(chunk)
                            written += len(chunk)
                            if progress:
                                progress(written, total)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except Exception:
                body = b""
            return self._http_error_detail(exc.code, body, rel)
        except Exception as exc:
            return f"下载失败：{exc}（路径：{rel}）"
        if not dest.exists() or dest.stat().st_size <= 0:
            return f"下载异常：本地文件为空（{rel}）"
        return f"已下载到 {dest}"

    @staticmethod
    def _http_error_detail(code: int, body: bytes | str | None, rel: str = "") -> str:
        text = ""
        if body:
            if isinstance(body, bytes):
                text = body.decode("utf-8", errors="replace")
            else:
                text = str(body)
        try:
            payload = json.loads(text) if text else {}
            if isinstance(payload, dict) and payload.get("error"):
                err = str(payload.get("error"))
                if rel and rel not in err:
                    return f"{err}（请求：{rel}）"
                return err
        except Exception:
            pass
        if code == 404:
            return (
                f"文件不存在或对方程序版本过旧无法下载（404）。"
                f"路径：{rel or '?'}。请确认双方都用最新源码运行，且对方已开启分享。"
            )
        if code == 401:
            return "密码错误或未授权（401）"
        return f"下载失败：HTTP {code}" + (f"（{rel}）" if rel else "")

    def list_tree(self, rel_path: str = "") -> tuple[str | None, list[dict[str, Any]]]:
        """Recursive file list under remote path (falls back to walk if API missing)."""
        if not self.connected:
            return "尚未连接", []
        try:
            data = self._request_json(
                self.base_url,
                "/api/list_tree",
                params={"path": rel_path or ""},
                password=self.password,
                timeout=60,
            )
            if data.get("ok"):
                return None, list(data.get("files") or [])
            # Explicit API error
            err = str(data.get("error") or "列出失败")
            if "未知接口" in err:
                return self._list_tree_fallback(rel_path)
            return err, []
        except _ApiHttpError as exc:
            # Old host without list_tree → walk via /api/list
            if exc.code == 404:
                return self._list_tree_fallback(rel_path)
            return str(exc), []
        except Exception as exc:
            msg = str(exc)
            if "404" in msg:
                return self._list_tree_fallback(rel_path)
            return msg, []

    def _list_tree_fallback(self, rel_path: str = "") -> tuple[str | None, list[dict[str, Any]]]:
        """BFS walk using /api/list when list_tree is unavailable."""
        files: list[dict[str, Any]] = []
        queue: list[str] = [(rel_path or "").replace("\\", "/").strip("/")]
        seen: set[str] = set()
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            err, entries = self.list_dir(cur)
            if err:
                if not files and cur == (rel_path or "").replace("\\", "/").strip("/"):
                    return err, []
                continue
            for entry in entries:
                name = str(entry.get("name") or "")
                if not name or name == "..":
                    continue
                child_rel = f"{cur}/{name}".strip("/") if cur else name
                # Prefer server-provided path when present
                if entry.get("path"):
                    child_rel = str(entry.get("path")).replace("\\", "/")
                if entry.get("is_dir"):
                    queue.append(child_rel)
                else:
                    files.append(
                        {
                            "name": name,
                            "path": child_rel,
                            "is_dir": False,
                            "size": int(entry.get("size") or 0),
                            "mtime": int(entry.get("mtime") or 0),
                        }
                    )
        return None, files

    def download_folder(
        self,
        rel_path: str,
        dest_dir: str | Path,
        *,
        sync: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> str:
        """Download entire remote folder (or sync: skip same-size existing files)."""
        if not self.connected:
            return "尚未连接"
        dest_root = Path(dest_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        err, files = self.list_tree(rel_path)
        if err:
            return err
        if not files:
            return "文件夹为空或无可下载文件。"
        ok = 0
        skipped = 0
        failed = 0
        base = (rel_path or "").replace("\\", "/").strip("/")
        for i, entry in enumerate(files):
            rpath = str(entry.get("path") or "")
            size = int(entry.get("size") or 0)
            if not rpath:
                continue
            # Map remote path under selected folder into local relative path
            if base and rpath.startswith(base + "/"):
                local_rel = rpath[len(base) + 1 :]
            elif base and rpath == base:
                local_rel = Path(rpath).name
            else:
                local_rel = rpath
            local_path = dest_root / local_rel
            if sync and local_path.is_file() and local_path.stat().st_size == size and size > 0:
                skipped += 1
                if progress:
                    progress(local_rel, i + 1, len(files))
                continue
            msg = self.download(rpath, local_path, resume=True)
            if msg.startswith("已下载"):
                ok += 1
            else:
                failed += 1
            if progress:
                progress(local_rel, i + 1, len(files))
        return f"文件夹完成：成功 {ok}，跳过 {skipped}，失败 {failed} → {dest_root}"

    def download_many(
        self,
        rel_paths: list[str],
        dest_dir: str | Path,
        *,
        sync: bool = False,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> str:
        dest_root = Path(dest_dir)
        dest_root.mkdir(parents=True, exist_ok=True)
        ok = skip = fail = 0
        for i, rel in enumerate(rel_paths):
            name = Path(rel.replace("\\", "/")).name
            dest = dest_root / name
            if sync and dest.is_file() and dest.stat().st_size > 0:
                skip += 1
                if progress:
                    progress(name, i + 1, len(rel_paths))
                continue
            msg = self.download(rel, dest, resume=True)
            if msg.startswith("已下载"):
                ok += 1
            else:
                fail += 1
            if progress:
                progress(name, i + 1, len(rel_paths))
        return f"批量下载：成功 {ok}，跳过 {skip}，失败 {fail} → {dest_root}"

    def upload(self, local_file: str | Path, remote_dir: str = "", progress: Callable[[int, int], None] | None = None) -> str:
        if not self.connected:
            return "尚未连接"
        src = Path(local_file)
        if not src.is_file():
            return "本地文件不存在"
        size = src.stat().st_size
        rel = (remote_dir or "").replace("\\", "/")
        url = (
            f"{self.base_url}/api/upload?"
            f"path={urllib.parse.quote(rel)}&name={urllib.parse.quote(src.name)}"
        )
        headers = {
            "Authorization": f"Bearer {self.password}",
            "Content-Type": "application/octet-stream",
            "X-Filename": src.name,
        }
        try:
            try:
                import requests

                # Pass a seekable file handle so requests sets Content-Length
                # (chunked encoding is not supported by our simple host server).
                with src.open("rb") as handle:
                    resp = requests.post(
                        url,
                        data=handle,
                        headers={**headers, "Content-Length": str(size)},
                        timeout=600,
                    )
                if progress:
                    progress(size, size)
                if resp.status_code != 200:
                    try:
                        payload = resp.json()
                        return str(payload.get("error") or f"HTTP {resp.status_code}")
                    except Exception:
                        return f"上传失败：HTTP {resp.status_code}"
                data = resp.json() if resp.content else {}
            except ImportError:
                body = src.read_bytes()
                req = urllib.request.Request(url, data=body, method="POST")
                for key, value in headers.items():
                    req.add_header(key, value)
                req.add_header("Content-Length", str(size))
                with urllib.request.urlopen(req, timeout=600) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(raw) if raw else {}
                if progress:
                    progress(size, size)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
                payload = json.loads(body)
                return str(payload.get("error") or f"HTTP {exc.code}")
            except Exception:
                return f"上传失败：HTTP {exc.code}"
        except Exception as exc:
            return f"上传失败：{exc}"
        if not data.get("ok"):
            return str(data.get("error") or "上传失败")
        return f"已上传 {src.name}"

    def mkdir(self, rel_path: str) -> str:
        if not self.connected:
            return "尚未连接"
        try:
            data = self._request_json(
                self.base_url,
                "/api/mkdir",
                params={"path": rel_path or ""},
                password=self.password,
                method="POST",
            )
        except Exception as exc:
            return str(exc)
        if not data.get("ok"):
            return str(data.get("error") or "创建失败")
        return "已创建文件夹"

    @staticmethod
    def _request_json(
        base: str,
        api_path: str,
        *,
        password: str,
        params: dict[str, str] | None = None,
        method: str = "GET",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = f"{base}{api_path}"
        if query:
            url = f"{url}?{query}"
        data = b"" if method.upper() == "POST" else None
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header("Authorization", f"Bearer {password}")
        if method.upper() == "POST":
            req.add_header("Content-Length", "0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                payload = json.loads(raw) if raw else {}
                if isinstance(payload, dict) and payload.get("error"):
                    raise _ApiHttpError(exc.code, str(payload.get("error"))) from exc
            except _ApiHttpError:
                raise
            except Exception:
                pass
            raise _ApiHttpError(exc.code, f"HTTP Error {exc.code}: {exc.reason}") from exc


class _ApiHttpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = int(code)
        self.message = message

    def __str__(self) -> str:
        return self.message


def format_size(n: int) -> str:
    value = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{n} B"
