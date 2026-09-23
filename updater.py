"""Check for application updates from GitHub Releases; optional download + run installer."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Bump when shipping a new installer (keep in sync with VERSION file).
APP_VERSION = "1.8.2"

# Public releases channel (organization repo)
GITHUB_REPO = "secure-artifacts/DesktopToolkit"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_LIST_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def get_app_version() -> str:
    """Prefer packaged VERSION file, then APP_VERSION constant."""
    try:
        from skin import bundle_root

        vf = bundle_root() / "VERSION"
        if vf.is_file():
            text = vf.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
    except Exception:
        pass
    return APP_VERSION


@dataclass
class UpdateCheckResult:
    ok: bool
    current: str
    latest: str
    has_update: bool
    message: str
    release_url: str
    download_url: str = ""
    asset_name: str = ""


def _normalize_version(text: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", text or "0")]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:4])


def _http_json(url: str, *, timeout: float, user_agent: str) -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": user_agent,
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _pick_release_payload(timeout: float, user_agent: str) -> dict:
    """
    Prefer /releases/latest, but also scan recent releases and take the
    highest non-prerelease tag — avoids stale 'latest' mirrors / flags.
    """
    latest_payload: dict = {}
    try:
        data = _http_json(RELEASES_API, timeout=timeout, user_agent=user_agent)
        if isinstance(data, dict):
            latest_payload = data
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        latest_payload = {}

    best = latest_payload
    best_ver = _normalize_version(
        str((latest_payload or {}).get("tag_name") or (latest_payload or {}).get("name") or "")
    )
    try:
        listing = _http_json(RELEASES_LIST_API, timeout=timeout, user_agent=user_agent)
        if isinstance(listing, list):
            for item in listing:
                if not isinstance(item, dict):
                    continue
                if item.get("draft") or item.get("prerelease"):
                    continue
                tag = str(item.get("tag_name") or item.get("name") or "").strip()
                ver = _normalize_version(tag)
                if ver > best_ver:
                    best = item
                    best_ver = ver
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        pass

    if not best:
        raise RuntimeError("无法从 GitHub 获取发布信息（网络或接口失败）")
    return best


def _select_asset(payload: dict) -> tuple[str, str]:
    """Prefer platform-matching assets (Mac must not pick Windows setup.exe)."""
    candidates: list[tuple[int, str, str]] = []
    is_mac = sys.platform == "darwin"
    is_win = sys.platform.startswith("win")
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        low = name.lower()
        if is_mac:
            # Prefer DMG (drag into Applications), then macos.zip
            if low.endswith(".dmg") and "macos" in low:
                candidates.append((0, url, name))
            elif low.endswith(".dmg"):
                candidates.append((1, url, name))
            elif "macos" in low and low.endswith(".zip"):
                candidates.append((2, url, name))
            elif low.endswith(".zip") and "windows" not in low and "win" not in low:
                candidates.append((3, url, name))
        elif is_win:
            if "setup" in low and low.endswith(".exe"):
                candidates.append((0, url, name))
            elif low.endswith(".exe"):
                candidates.append((1, url, name))
            elif "portable" in low and low.endswith(".zip"):
                candidates.append((2, url, name))
            elif low.endswith(".zip") or low.endswith(".7z"):
                candidates.append((3, url, name))
        else:
            if low.endswith(".zip") or low.endswith(".7z") or low.endswith(".exe"):
                candidates.append((5, url, name))
    if not candidates:
        return "", ""
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1], candidates[0][2]


def check_for_update(timeout: float = 8.0) -> UpdateCheckResult:
    """Query GitHub latest release. Network failures return ok=False (no crash)."""
    current = get_app_version()
    ua = f"DesktopToolkit/{current}"
    try:
        payload = _pick_release_payload(timeout=timeout, user_agent=ua)
    except Exception as exc:
        return UpdateCheckResult(
            ok=False,
            current=current,
            latest="",
            has_update=False,
            message=f"检查更新失败：{exc}",
            release_url=RELEASES_PAGE,
        )

    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    latest = tag.lstrip("vV")
    html_url = str(payload.get("html_url") or RELEASES_PAGE)
    download, asset_name = _select_asset(payload)

    if not latest:
        return UpdateCheckResult(
            ok=False,
            current=current,
            latest="",
            has_update=False,
            message="未找到发布版本信息。",
            release_url=html_url,
        )

    has_update = _normalize_version(latest) > _normalize_version(current)
    if has_update:
        msg = (
            f"发现新版本 {latest}（当前 {current}）。\n"
            f"推荐下载：{asset_name or '见 GitHub Releases'}"
        )
    else:
        msg = (
            f"已是最新版本。\n"
            f"当前：{current}\n"
            f"远程最新：{latest}\n\n"
            f"若界面仍像旧版，请完全退出后从 GitHub Latest 重新安装。"
        )
    return UpdateCheckResult(
        ok=True,
        current=current,
        latest=latest,
        has_update=has_update,
        message=msg,
        release_url=html_url,
        download_url=download,
        asset_name=asset_name,
    )


def download_update(
    url: str,
    *,
    dest_dir: Path | None = None,
    filename: str | None = None,
    timeout: float = 120.0,
    progress_cb=None,
) -> Path:
    """Download installer/asset to a local file. Raises on failure."""
    if not url or not str(url).startswith("http"):
        raise ValueError("没有可下载的安装包地址，请打开下载页手动获取。")
    low = str(url).lower()
    if "/releases/tag/" in low and not any(
        low.endswith(ext) for ext in (".exe", ".zip", ".7z", ".dmg")
    ):
        raise ValueError("没有可下载的安装包地址，请打开下载页手动获取。")
    dest_dir = dest_dir or Path(tempfile.gettempdir()) / "DesktopToolkitUpdates"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = (filename or "").strip() or urllib_parse_unquote_name(url) or "DesktopToolkit-update.bin"
    name = re.sub(r"[^\w.\-]+", "_", name)[:120] or "update.bin"
    dest = dest_dir / name
    headers = {"User-Agent": f"DesktopToolkit/{get_app_version()}"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        chunk = 256 * 1024
        with dest.open("wb") as f:
            while True:
                block = resp.read(chunk)
                if not block:
                    break
                f.write(block)
                done += len(block)
                if progress_cb and total > 0:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass
    if not dest.is_file() or dest.stat().st_size < 1024:
        raise RuntimeError("下载文件无效或过小。")
    return dest


def urllib_parse_unquote_name(url: str) -> str:
    try:
        from urllib.parse import unquote, urlparse

        path = urlparse(url).path
        return unquote(path.rsplit("/", 1)[-1])
    except Exception:
        return "update.bin"


def _find_app_bundle(root: Path) -> Path | None:
    if root.is_dir() and root.name.endswith(".app"):
        return root
    if not root.is_dir():
        return None
    for child in root.rglob("*.app"):
        if child.is_dir():
            return child
    return None


def _install_mac_zip(zip_path: Path) -> Path:
    """Unzip macOS zip and copy DesktopToolkit.app into /Applications (fallback ~/Applications)."""
    extract_dir = zip_path.parent / f"{zip_path.stem}_extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)
    app = _find_app_bundle(extract_dir)
    if app is None:
        # Open Finder so user can drag manually
        subprocess.Popen(["open", str(extract_dir)])
        raise RuntimeError(
            f"压缩包内未找到 DesktopToolkit.app，已打开文件夹：{extract_dir}"
        )
    targets = [Path("/Applications"), Path.home() / "Applications"]
    last_err: Exception | None = None
    for parent in targets:
        try:
            parent.mkdir(parents=True, exist_ok=True)
            dest = parent / app.name
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            # ditto preserves macOS resource forks / signatures better than shutil.copytree
            subprocess.run(["ditto", str(app), str(dest)], check=True)
            subprocess.Popen(["open", str(parent)])
            return dest
        except Exception as exc:
            last_err = exc
            continue
    subprocess.Popen(["open", str(app.parent)])
    raise RuntimeError(
        f"无法自动复制到「应用程序」：{last_err}。已打开 .app 所在文件夹，请手动拖入「应用程序」。"
    )


def launch_installer(path: Path) -> None:
    """Start setup.exe / open DMG / install Mac zip into Applications."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if sys.platform == "win32":
        subprocess.Popen(
            [str(path)],
            cwd=str(path.parent),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    elif sys.platform == "darwin":
        low = path.name.lower()
        if low.endswith(".dmg"):
            # Same UX as RustDesk: open DMG, user drags app into Applications
            subprocess.Popen(["open", str(path)], cwd=str(path.parent))
        elif low.endswith(".zip"):
            _install_mac_zip(path)
        else:
            subprocess.Popen(["open", str(path)], cwd=str(path.parent))
    else:
        subprocess.Popen([str(path)], cwd=str(path.parent))
