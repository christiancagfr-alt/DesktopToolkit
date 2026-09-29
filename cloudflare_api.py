"""Cloudflare helpers for P2P signaling Worker: list / resolve URL / local wrangler deploy."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable


API = "https://api.cloudflare.com/client/v4"
# Pinned Node LTS for portable auto-install (PyInstaller apps often miss system PATH)
_NODE_LTS_VER = "v22.14.0"
_NODE_WIN_ZIP = (
    f"https://nodejs.org/dist/{_NODE_LTS_VER}/node-{_NODE_LTS_VER}-win-x64.zip"
)


def normalize_token(token: str) -> str:
    t = (token or "").strip().strip('"').strip("'")
    # Users sometimes paste "Bearer xxx"
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def normalize_account_id(account_id: str) -> str:
    a = (account_id or "").strip().strip('"').strip("'")
    # Allow dashed paste; CF ids are 32 hex
    a = a.replace("-", "").replace(" ", "")
    return a


def _looks_like_account_id(account_id: str) -> bool:
    return bool(re.fullmatch(r"[a-fA-F0-9]{32}", account_id or ""))


def _request(
    token: str,
    method: str,
    path: str,
    *,
    data: dict | None = None,
    timeout: float = 12.0,
) -> dict:
    token = normalize_token(token)
    if not token:
        raise RuntimeError("API Token 为空")
    url = path if path.startswith("http") else f"{API}{path}"
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "DesktopToolkit/1.9",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            raise RuntimeError(f"Cloudflare HTTP {e.code}: {raw[:300]}") from e
        errs = payload.get("errors") or []
        msg = errs[0].get("message") if errs else raw[:300]
        code = errs[0].get("code") if errs else None
        hint = ""
        if e.code in (401, 403) or code in (9106, 9109, 10000):
            hint = (
                "（请用 API Token，不要用 Global API Key；"
                "权限需含 Account.Account Settings:Read 与 Workers Scripts:Read）"
            )
        raise RuntimeError(f"Cloudflare: {msg}{hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误：{e.reason}") from e
    if not payload.get("success", True):
        errs = payload.get("errors") or []
        msg = errs[0].get("message") if errs else "unknown error"
        raise RuntimeError(f"Cloudflare: {msg}")
    return payload


def verify_token(token: str) -> dict[str, Any]:
    """Optional token check. Account-scoped tokens often cannot call /user/* — keep short timeout."""
    try:
        data = _request(token, "GET", "/user/tokens/verify", timeout=5.0)
        result = data.get("result") or {}
        return {"ok": True, "status": str(result.get("status") or "active"), "via": "verify"}
    except Exception as e:
        return {"ok": False, "error": str(e), "via": "verify"}


def list_accounts(token: str) -> list[dict[str, str]]:
    data = _request(token, "GET", "/accounts?per_page=50", timeout=12.0)
    out = []
    for row in data.get("result") or []:
        out.append({"id": str(row.get("id") or ""), "name": str(row.get("name") or "")})
    return [a for a in out if a["id"]]


def detect_account(token: str, account_id: str = "") -> dict[str, Any]:
    """
    Validate token by listing accounts (single request — skip /user/tokens/verify which
    often 403s or hangs for account-scoped tokens).
    Prefers an account that already has workers.dev / Workers scripts when Account ID empty.
    """
    token = normalize_token(token)
    want = normalize_account_id(account_id)
    accounts = list_accounts(token)
    if not accounts:
        raise RuntimeError(
            "Token 能访问 API，但列不出账户。请确认 Token 权限含 Account.Account Settings:Read"
        )
    if want:
        chosen = None
        for a in accounts:
            if normalize_account_id(a["id"]) == want:
                chosen = a
                break
        if chosen is None:
            # Still allow explicit id if user knows it (token may be account-scoped to one)
            chosen = {"id": want, "name": "(指定 Account ID)"}
    else:
        chosen = pick_best_account(token, accounts)
    return {
        "account": chosen,
        "accounts": accounts,
        "verify": {"ok": True, "via": "accounts"},
    }


def list_worker_scripts(token: str, account_id: str) -> list[str]:
    account_id = normalize_account_id(account_id)
    if not account_id:
        raise RuntimeError("Account ID 为空")
    data = _request(token, "GET", f"/accounts/{account_id}/workers/scripts")
    names = []
    for row in data.get("result") or []:
        n = str(row.get("id") or row.get("name") or "").strip()
        if n:
            names.append(n)
    # Prefer toolkit / p2p / signal names first for handshake picking
    def _rank(n: str) -> tuple:
        low = n.lower()
        score = 0
        for key in ("p2p", "signal", "handshake", "desktop-toolkit", "toolkit", "relay"):
            if key in low:
                score -= 10
        return (score, low)

    names.sort(key=_rank)
    return names


def workers_subdomain(token: str, account_id: str) -> str:
    account_id = normalize_account_id(account_id)
    data = _request(token, "GET", f"/accounts/{account_id}/workers/subdomain")
    result = data.get("result") or {}
    return str(result.get("subdomain") or "").strip()


def ensure_workers_subdomain(
    token: str,
    account_id: str,
    *,
    preferred: str = "",
    on_log: Callable[[str], None] | None = None,
) -> str:
    """
    Return existing workers.dev account subdomain, or create one if missing (GET 404).
    Requires Token permission: Workers Scripts Write/Edit.
    """
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    account_id = normalize_account_id(account_id)
    try:
        sub = workers_subdomain(token, account_id)
        if sub:
            log(f"workers.dev 子域已存在：{sub}")
            return sub
    except Exception as e:
        msg = str(e).lower()
        if "10000" in msg or "authentication" in msg or "unauthorized" in msg:
            raise RuntimeError(
                f"无法读取 workers.dev 子域（鉴权失败）：{e}\n"
                "请确认 Token 对该 Account 有 Workers Scripts:Edit 权限。"
            ) from e
        # 404 / not found → create below
        log(f"账户尚未设置 workers.dev 子域（{e}），正在自动创建…")

    base = re.sub(r"[^a-z0-9-]", "", (preferred or f"dt-{account_id[:8]}").lower()).strip("-")
    if not base:
        base = f"dt-{account_id[:8]}"
    candidates = [base, f"{base}-p2p", f"desktop-toolkit-{account_id[:6]}"]
    last_err = ""
    for name in candidates:
        try:
            data = _request(
                token,
                "PUT",
                f"/accounts/{account_id}/workers/subdomain",
                data={"subdomain": name},
            )
            sub = str((data.get("result") or {}).get("subdomain") or name).strip()
            log(f"已创建 workers.dev 子域：{sub}")
            return sub
        except Exception as e:
            last_err = str(e)
            # already has subdomain / name taken → try next or re-GET
            if "account_has_subdomain" in last_err.lower():
                try:
                    return workers_subdomain(token, account_id)
                except Exception:
                    pass
            continue
    raise RuntimeError(
        "自动创建 workers.dev 子域失败。请到 Cloudflare 控制台 → Workers → 设置 Your subdomain。\n"
        f"详情：{last_err}"
    )


def pick_best_account(token: str, accounts: list[dict[str, str]], prefer_id: str = "") -> dict[str, str]:
    """Prefer account that already has a workers.dev subdomain / can list scripts."""
    if not accounts:
        raise RuntimeError("没有可用账户")
    prefer = normalize_account_id(prefer_id)
    if prefer:
        for a in accounts:
            if normalize_account_id(a.get("id") or "") == prefer:
                return a

    scored: list[tuple[int, dict[str, str]]] = []
    for a in accounts:
        aid = normalize_account_id(a.get("id") or "")
        score = 0
        try:
            if workers_subdomain(token, aid):
                score += 100
        except Exception:
            pass
        try:
            if list_worker_scripts(token, aid):
                score += 20
        except Exception:
            pass
        scored.append((score, a))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("name") or "").lower()))
    return scored[0][1]


def worker_https_url(script_name: str, subdomain: str) -> str:
    name = script_name.strip()
    sub = subdomain.strip()
    if not name or not sub:
        return ""
    return f"https://{name}.{sub}.workers.dev"


def resolve_handshake_url(token: str, account_id: str, script_name: str) -> str:
    sub = workers_subdomain(token, account_id)
    url = worker_https_url(script_name, sub)
    if not url:
        raise RuntimeError(
            "无法拼出 workers.dev 地址：账户可能还没启用 Workers 子域。"
            "请先在 Cloudflare Dashboard → Workers → 设置子域，或点「一键新建握手」。"
        )
    return url


def list_handshake_candidates(token: str, account_id: str) -> list[dict[str, str]]:
    """Return [{name, url}] for each Worker script (url may be empty if subdomain missing)."""
    names = list_worker_scripts(token, account_id)
    sub = ""
    try:
        sub = workers_subdomain(token, account_id)
    except Exception:
        sub = ""
    out = []
    for n in names:
        out.append({"name": n, "url": worker_https_url(n, sub) if sub else ""})
    return out


def cloudflare_dir() -> Path:
    try:
        from skin import bundle_root

        p = bundle_root() / "cloudflare"
        if p.is_dir():
            return p
    except Exception:
        pass
    return Path(__file__).resolve().parent / "cloudflare"


def _tools_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    root = Path(base) / "DesktopToolkit" / "tools"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _node_candidates() -> list[Path]:
    """Dirs that may contain node.exe / npx.cmd even when PATH is stripped (frozen app)."""
    homes = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "nodejs",
        _tools_root() / f"node-{_NODE_LTS_VER}-win-x64",
        _tools_root() / "node",
    ]
    # Also whatever shutil finds via current PATH
    which_node = shutil.which("node")
    if which_node:
        homes.insert(0, Path(which_node).resolve().parent)
    out: list[Path] = []
    seen: set[str] = set()
    for d in homes:
        try:
            key = str(d.resolve()).lower()
        except Exception:
            key = str(d).lower()
        if key in seen or not d:
            continue
        seen.add(key)
        out.append(d)
    return out


def _resolve_node_bins() -> tuple[Path, Path, Path] | None:
    """Return (node, npm, npx) paths if a usable Node install is found."""
    for d in _node_candidates():
        if sys.platform == "win32":
            node = d / "node.exe"
            npm = d / "npm.cmd"
            npx = d / "npx.cmd"
        else:
            node = d / "node"
            npm = d / "npm"
            npx = d / "npx"
        if node.is_file() and npx.is_file():
            return node, npm, npx
    # Fallback: bare which (POSIX)
    node_w = shutil.which("node")
    npx_w = shutil.which("npx")
    npm_w = shutil.which("npm")
    if node_w and npx_w:
        return Path(node_w), Path(npm_w or npx_w), Path(npx_w)
    return None


def _download_file(url: str, dest: Path, *, on_log: Callable[[str], None] | None = None) -> None:
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"下载：{url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "DesktopToolkit/1.9", "Accept": "*/*"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total > 0 and done % (1024 * 1024 * 5) < 256 * 1024:
                log(f"下载中… {done // (1024 * 1024)} / {total // (1024 * 1024)} MB")
    tmp.replace(dest)
    log(f"已下载：{dest.name}")


def _install_node_portable(on_log: Callable[[str], None] | None = None) -> tuple[Path, Path, Path]:
    """Download Node LTS zip into LOCALAPPDATA\\DesktopToolkit\\tools (no admin)."""
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    if sys.platform != "win32":
        raise RuntimeError(
            "未找到 Node.js。请先安装：https://nodejs.org/ （安装后重启软件再试一键新建）"
        )

    tools = _tools_root()
    folder_name = f"node-{_NODE_LTS_VER}-win-x64"
    target = tools / folder_name
    node = target / "node.exe"
    npx = target / "npx.cmd"
    if node.is_file() and npx.is_file():
        log(f"使用已缓存的 Node：{target}")
        return node, target / "npm.cmd", npx

    log(f"未检测到 Node/npm，正在自动安装便携版 {_NODE_LTS_VER}…")
    zip_path = tools / f"{folder_name}.zip"
    try:
        _download_file(_NODE_WIN_ZIP, zip_path, on_log=on_log)
    except Exception as e:
        # winget fallback
        winget = shutil.which("winget")
        if winget:
            log("直链下载失败，尝试 winget 安装 Node.js LTS…")
            r = subprocess.run(
                [
                    winget,
                    "install",
                    "-e",
                    "--id",
                    "OpenJS.NodeJS.LTS",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                    "--disable-interactivity",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            log((r.stdout or "")[-400:])
            found = _resolve_node_bins()
            if found:
                return found
        raise RuntimeError(f"自动安装 Node.js 失败：{e}") from e

    log("正在解压 Node…")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(tools)
    if not node.is_file():
        # zip root may nest differently
        matches = list(tools.glob("node-*/node.exe"))
        if matches:
            target = matches[0].parent
            node = target / "node.exe"
            npx = target / "npx.cmd"
    if not node.is_file() or not npx.is_file():
        raise RuntimeError(f"Node 解压后未找到 node/npx：{tools}")
    try:
        zip_path.unlink(missing_ok=True)
    except Exception:
        pass
    log(f"Node 已就绪：{node}")
    return node, target / "npm.cmd", npx


def ensure_node_bins(
    *,
    on_log: Callable[[str], None] | None = None,
) -> tuple[Path, Path, Path]:
    """Find system/portable Node, or auto-install a portable copy."""
    found = _resolve_node_bins()
    if found:
        if on_log:
            try:
                on_log(f"已找到 Node：{found[0]}")
            except Exception:
                pass
        return found
    return _install_node_portable(on_log=on_log)


def _npx_cli_js(node: Path) -> Path | None:
    """Prefer running npx via node + npx-cli.js (avoids broken .cmd quoting on Windows)."""
    cand = node.parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
    return cand if cand.is_file() else None


def _run_npx(
    node: Path,
    npx: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run npx reliably on Windows/macOS/Linux."""
    cli = _npx_cli_js(node)
    if cli is not None:
        cmd = [str(node), str(cli), *args]
    elif sys.platform == "win32" and npx.suffix.lower() in {".cmd", ".bat"}:
        # Fallback: cmd.exe without /s (keeps quoted path intact)
        cmdline = subprocess.list2cmdline([str(npx), *args])
        cmd = ["cmd.exe", "/d", "/c", cmdline]
    else:
        cmd = [str(npx), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        shell=False,
    )


def deploy_signaling_worker(
    *,
    on_log: Callable[[str], None] | None = None,
    api_token: str = "",
    account_id: str = "",
) -> str:
    """
    Ensure Node/npm, then run `npx wrangler deploy` in cloudflare/.
    Prefer CLOUDFLARE_API_TOKEN from UI so browser login is unnecessary.
    Returns https://…workers.dev URL. Raises RuntimeError on failure.
    """
    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    cf = cloudflare_dir()
    if not cf.is_dir():
        raise RuntimeError(f"找不到 cloudflare 目录：{cf}")
    log(f"部署目录：{cf}")

    try:
        node, npm, npx = ensure_node_bins(on_log=log)
    except Exception as e:
        raise RuntimeError(f"未找到 npm / npx，自动安装也失败了：{e}") from e

    env = os.environ.copy()
    # Put Node dir first so child tools resolve correctly
    node_dir = str(node.parent)
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    # Strip npm configs that break newer npm (e.g. NPM_CONFIG_YES → "Unknown env config yes")
    for k in list(env.keys()):
        if k.upper().startswith("NPM_CONFIG_"):
            env.pop(k, None)

    token = normalize_token(api_token)
    acct = normalize_account_id(account_id)
    if acct and not _looks_like_account_id(acct):
        raise RuntimeError(
            "Account ID 格式不对：应为 32 位十六进制（在 Cloudflare 控制台 Workers 右侧可见）。\n"
            "请勿把 API Token 填进 Account ID 框。"
        )
    if token:
        env["CLOUDFLARE_API_TOKEN"] = token
        log("已使用界面填写的 API Token（无需浏览器登录）")
    if acct:
        env["CLOUDFLARE_ACCOUNT_ID"] = acct

    # Pre-check token via Cloudflare HTTP API (clearer errors than wrangler whoami)
    if token:
        log("正在校验 API Token…")
        try:
            info = detect_account(token, acct)
            if not acct:
                acct = normalize_account_id(str((info.get("account") or {}).get("id") or ""))
                if acct:
                    env["CLOUDFLARE_ACCOUNT_ID"] = acct
                    aname = str((info.get("account") or {}).get("name") or "")
                    log(f"已自动使用账户：{aname}（{acct[:8]}…）")
                    others = info.get("accounts") or []
                    if len(others) > 1:
                        log(f"提示：Token 下有 {len(others)} 个账户，已优先选有 Workers 的那个")
        except Exception as e:
            raise RuntimeError(
                f"API Token 校验失败：{e}\n"
                "请用 API Token（不要用 Global API Key），权限需含：\n"
                "· Account Settings: Read\n"
                "· Workers Scripts: Edit"
            ) from e
        if acct:
            # First-time accounts often have no workers.dev subdomain → wrangler then fails
            # with confusing "Authentication error [code: 10000]". Create it first.
            try:
                ensure_workers_subdomain(token, acct, preferred=f"dt-{acct[:8]}", on_log=log)
            except Exception as e:
                raise RuntimeError(
                    f"准备 workers.dev 子域失败：{e}\n"
                    "也可手动：Cloudflare 控制台 → Workers & Pages → 设置 Your subdomain"
                ) from e
    else:
        # No token → need wrangler OAuth login
        log("检查 Cloudflare 登录状态…")
        try:
            who = _run_npx(
                node, npx, ["--yes", "wrangler@4", "whoami"], cwd=cf, env=env, timeout=180
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "未找到 npm / npx。请先安装 Node.js（含 npm），再试一键新建。"
            ) from e
        except subprocess.TimeoutExpired:
            raise RuntimeError("wrangler whoami 超时。请检查网络后重试。")

        who_out = ((who.stdout or "") + "\n" + (who.stderr or "")).lower()
        # Ignore npm noise lines when deciding auth state
        auth_lines = "\n".join(
            ln
            for ln in who_out.splitlines()
            if "npm warn" not in ln and "unknown env config" not in ln
        )
        need_login = who.returncode != 0 or (
            "not authenticated" in auth_lines or "not logged" in auth_lines
        )
        if need_login:
            log("未登录 Cloudflare，请在弹出的浏览器中完成登录…")
            login = _run_npx(
                node, npx, ["--yes", "wrangler@4", "login"], cwd=cf, env=env, timeout=600
            )
            if login.returncode != 0:
                raise RuntimeError(
                    "Cloudflare 登录失败。请在上方填写 API Token 后重试，"
                    "或手动执行：npx wrangler login\n"
                    + (login.stderr or login.stdout or "")[:400]
                )

    log("正在 wrangler deploy…（首次会自动下载 wrangler，可能需 1–2 分钟）")
    try:
        dep = _run_npx(
            node, npx, ["--yes", "wrangler@4", "deploy"], cwd=cf, env=env, timeout=900
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("wrangler deploy 超时。请检查网络后重试。")
    out = (dep.stdout or "") + "\n" + (dep.stderr or "")
    # Drop noisy npm warnings from the log snippet shown to users
    clean_lines = [
        ln
        for ln in out.splitlines()
        if "npm warn" not in ln.lower() and "unknown env config" not in ln.lower()
    ]
    clean_out = "\n".join(clean_lines) if clean_lines else out
    log(clean_out[-800:] if len(clean_out) > 800 else clean_out)
    if dep.returncode != 0:
        hint = ""
        low = clean_out.lower()
        if "authentication" in low or "unauthorized" in low or "401" in low or "403" in low:
            hint = (
                "\n\n鉴权失败时请检查：Token 是否含 Workers Scripts:Edit；"
                "Account ID 是否为 32 位账户 ID（不要把 Token 填进 Account ID）。"
            )
        raise RuntimeError("部署失败：\n" + clean_out[-600:] + hint)

    m = re.search(r"https://[a-zA-Z0-9._-]+\.workers\.dev", out)
    if m:
        return m.group(0).rstrip("/")

    # Fallback: compose URL via API when token is available
    name = "desktop-toolkit-p2p"
    try:
        text = (cf / "wrangler.toml").read_text(encoding="utf-8", errors="replace")
        mm = re.search(r'name\s*=\s*"([^"]+)"', text)
        if mm:
            name = mm.group(1)
    except Exception:
        pass
    if token and acct:
        try:
            url = resolve_handshake_url(token, acct, name)
            if url:
                return url
        except Exception as e:
            log(f"API 解析地址失败：{e}")
    raise RuntimeError(
        f"部署似乎成功，但未从输出解析到 workers.dev 地址。\n"
        f"请到 Cloudflare 控制台查看 Worker「{name}」的地址后填入。"
    )


def fetch_account_workers_usage_today(
    token: str,
    account_id: str,
) -> dict[str, Any]:
    """
    Approximate account-level Workers invocations for today (UTC) via GraphQL.
    Requires a token with Account Analytics Read.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    query = {
        "query": """
        query ($accountTag: string!, $filter: AccountWorkersInvocationsAdaptiveFilter_InputObject) {
          viewer {
            accounts(filter: {accountTag: $accountTag}) {
              workersInvocationsAdaptive(limit: 1, filter: $filter) {
                sum { requests }
              }
            }
          }
        }
        """,
        "variables": {
            "accountTag": account_id,
            "filter": {
                "datetime_geq": f"{day}T00:00:00Z",
                "datetime_leq": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
    }
    # Cloudflare GraphQL endpoint
    url = "https://api.cloudflare.com/client/v4/graphql"
    body = json.dumps(query).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "DesktopToolkit/1.9",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    if payload.get("errors"):
        err = payload["errors"][0].get("message") if payload["errors"] else "GraphQL error"
        raise RuntimeError(str(err))
    used = 0
    try:
        accounts = payload["data"]["viewer"]["accounts"]
        rows = accounts[0]["workersInvocationsAdaptive"]
        if rows:
            used = int(rows[0]["sum"]["requests"] or 0)
    except Exception:
        used = 0
    limit = 100_000
    return {
        "ok": True,
        "day_utc": day,
        "account_requests_today": used,
        "daily_limit": limit,
        "remaining": max(0, limit - used),
        "source": "cloudflare_graphql",
        "note": "账户级 Workers 调用量（今日 UTC），非单个 Worker 近似计数。",
    }
