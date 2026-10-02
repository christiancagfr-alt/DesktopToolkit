"""Check for application updates from GitHub Releases; optional download + run installer."""

from __future__ import annotations

import hashlib
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
APP_VERSION = "1.9.6"

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
    sha256_url: str = ""
    sha256_expected: str = ""


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
    highest non-prerelease tag 鈥?avoids stale 'latest' mirrors / flags.
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
        raise RuntimeError("鏃犳硶浠?GitHub 鑾峰彇鍙戝竷淇℃伅锛堢綉缁滄垨鎺ュ彛澶辫触锛?)
    return best


def _select_asset(payload: dict) -> tuple[str, str, str]:
    """Prefer platform-matching assets. Returns (url, name, sha256_url)."""
    candidates: list[tuple[int, str, str]] = []
    sha_by_name: dict[str, str] = {}
    is_mac = sys.platform == "darwin"
    is_win = sys.platform.startswith("win")
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not url:
            continue
        low = name.lower()
        if low.endswith(".sha256"):
            sha_by_name[name[:-7]] = url  # strip .sha256 鈫?original asset name key
            sha_by_name[name] = url
            continue
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
        return "", "", ""
    candidates.sort(key=lambda x: x[0])
    url, name = candidates[0][1], candidates[0][2]
    sha_url = sha_by_name.get(name) or sha_by_name.get(f"{name}.sha256") or ""
    return url, name, sha_url


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
            message=f"妫€鏌ユ洿鏂板け璐ワ細{exc}",
            release_url=RELEASES_PAGE,
        )

    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    latest = tag.lstrip("vV")
    html_url = str(payload.get("html_url") or RELEASES_PAGE)
    download, asset_name, sha256_url = _select_asset(payload)
    sha256_expected = ""
    if sha256_url and _update_url_allowed(sha256_url):
        try:
            sha256_expected = _fetch_sha256_text(sha256_url, timeout=timeout, user_agent=ua)
        except Exception:
            sha256_expected = ""

    if not latest:
        return UpdateCheckResult(
            ok=False,
            current=current,
            latest="",
            has_update=False,
            message="鏈壘鍒板彂甯冪増鏈俊鎭€?,
            release_url=html_url,
        )

    has_update = _normalize_version(latest) > _normalize_version(current)
    if has_update:
        msg = (
            f"鍙戠幇鏂扮増鏈?{latest}锛堝綋鍓?{current}锛夈€俓n"
            f"鎺ㄨ崘涓嬭浇锛歿asset_name or '瑙?GitHub Releases'}"
        )
        if sha256_expected:
            msg += "\n宸查檮甯?SHA256 鏍￠獙銆?
    else:
        msg = (
            f"宸叉槸鏈€鏂扮増鏈€俓n"
            f"褰撳墠锛歿current}\n"
            f"杩滅▼鏈€鏂帮細{latest}\n\n"
            f"鑻ョ晫闈粛鍍忔棫鐗堬紝璇峰畬鍏ㄩ€€鍑哄悗浠?GitHub Latest 閲嶆柊瀹夎銆?
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
        sha256_url=sha256_url,
        sha256_expected=sha256_expected,
    )


_ALLOWED_UPDATE_HOSTS = frozenset(
    {
        "github.com",
        "www.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)


def _update_url_allowed(url: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("https",):
            return False
        host = (parsed.hostname or "").lower()
        if host in _ALLOWED_UPDATE_HOSTS:
            return True
        # Allow GitHub release CDN hostnames under githubusercontent.com
        if host.endswith(".githubusercontent.com"):
            return True
        return False
    except Exception:
        return False


def _fetch_sha256_text(url: str, *, timeout: float, user_agent: str) -> str:
    """Download a .sha256 sidecar and return the hex digest (64 chars)."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    # Formats: "<hex>  filename" or bare "<hex>"
    first = (text.split() or [""])[0].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", first):
        raise ValueError(f"鏃犳硶瑙ｆ瀽 SHA256 鏂囦欢鍐呭锛歿text[:80]!r}")
    return first


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def download_update(
    url: str,
    *,
    dest_dir: Path | None = None,
    filename: str | None = None,
    timeout: float = 120.0,
    progress_cb=None,
    expected_sha256: str = "",
    sha256_url: str = "",
    require_sha256: bool = True,
) -> Path:
    """Download installer/asset to a local file. Raises on failure.

    When *require_sha256* is True (default), a matching GitHub ``.sha256``
    sidecar must verify; otherwise the download is deleted and rejected.
    """
    if not url or not str(url).startswith("http"):
        raise ValueError("娌℃湁鍙笅杞界殑瀹夎鍖呭湴鍧€锛岃鎵撳紑涓嬭浇椤垫墜鍔ㄨ幏鍙栥€?)
    if not _update_url_allowed(url):
        raise ValueError("鏇存柊鍦板潃涓嶅湪鍏佽鐨?GitHub 鍙戝竷鍩熷悕鍐咃紝宸叉嫆缁濅笅杞姐€?)
    low = str(url).lower()
    if "/releases/tag/" in low and not any(
        low.endswith(ext) for ext in (".exe", ".zip", ".7z", ".dmg")
    ):
        raise ValueError("娌℃湁鍙笅杞界殑瀹夎鍖呭湴鍧€锛岃鎵撳紑涓嬭浇椤垫墜鍔ㄨ幏鍙栥€?)
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
        raise RuntimeError("涓嬭浇鏂囦欢鏃犳晥鎴栬繃灏忋€?)

    expect = (expected_sha256 or "").strip().lower()
    if not expect and sha256_url:
        if not _update_url_allowed(sha256_url):
            raise RuntimeError("SHA256 鏍￠獙鍦板潃涓嶅湪鍏佽鍩熷悕鍐呫€?)
        expect = _fetch_sha256_text(
            sha256_url, timeout=min(30.0, timeout), user_agent=headers["User-Agent"]
        )
    if require_sha256 and not expect:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            "鍙戝竷鍖呯己灏?SHA256 鏍￠獙鏂囦欢锛屽凡鎷掔粷瀹夎銆傝浠?GitHub Releases 椤甸潰鎵嬪姩涓嬭浇銆?
        )
    if expect:
        actual = file_sha256(dest)
        if actual != expect:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"SHA256 鏍￠獙澶辫触锛堟枃浠跺彲鑳借绡℃敼鎴栦笅杞戒笉瀹屾暣锛夈€俓n鏈熸湜 {expect}\n瀹為檯 {actual}"
            )
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
    from zip_safe import safe_extractall

    with zipfile.ZipFile(zip_path, "r") as zf:
        safe_extractall(zf, extract_dir)
    app = _find_app_bundle(extract_dir)
    if app is None:
        # Open Finder so user can drag manually
        subprocess.Popen(["open", str(extract_dir)])
        raise RuntimeError(
            f"鍘嬬缉鍖呭唴鏈壘鍒?DesktopToolkit.app锛屽凡鎵撳紑鏂囦欢澶癸細{extract_dir}"
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
        f"鏃犳硶鑷姩澶嶅埗鍒般€屽簲鐢ㄧ▼搴忋€嶏細{last_err}銆傚凡鎵撳紑 .app 鎵€鍦ㄦ枃浠跺す锛岃鎵嬪姩鎷栧叆銆屽簲鐢ㄧ▼搴忋€嶃€?
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
