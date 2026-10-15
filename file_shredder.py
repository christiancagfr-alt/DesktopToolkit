"""Secure file shredding (Eraser-style multi-pass overwrite, no Recycle Bin).

Overwrites file contents in place, optionally renames, then unlinks. Directories
are shredded recursively (files first), then empty folders are removed.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ProgressCb = Callable[[str, float], None]  # message, 0..1 overall
LogCb = Callable[[str], None]

DEFAULT_PASSES = 3
MIN_PASSES = 1
MAX_PASSES = 35
CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class ShredItemResult:
    path: str
    ok: bool
    detail: str = ""


@dataclass
class ShredReport:
    results: list[ShredItemResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def summary(self) -> str:
        if self.cancelled:
            return f"已取消。成功 {self.ok_count}，失败 {self.fail_count}"
        return f"粉碎完成：成功 {self.ok_count}，失败 {self.fail_count}"


def clamp_passes(n: int | None) -> int:
    try:
        v = int(n if n is not None else DEFAULT_PASSES)
    except (TypeError, ValueError):
        v = DEFAULT_PASSES
    return max(MIN_PASSES, min(MAX_PASSES, v))


def _is_protected_path(path: Path) -> bool:
    """Refuse obvious system / self paths."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    s = str(resolved).replace("/", "\\").lower()
    # Running executable / bundle
    try:
        exe = Path(sys.executable).resolve()
        if resolved == exe or resolved in exe.parents:
            return True
    except Exception:
        pass
    try:
        here = Path(__file__).resolve().parent
        if resolved == here or here in resolved.parents and resolved.name in {
            "file_shredder.py",
            "main.py",
            "DesktopToolkit.exe",
        }:
            return True
    except Exception:
        pass
    if sys.platform == "win32":
        windir = (os.environ.get("WINDIR") or r"C:\Windows").lower()
        pf = (os.environ.get("ProgramFiles") or r"C:\Program Files").lower()
        pf86 = (os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)").lower()
        for root in (windir, pf, pf86):
            if s == root or s.startswith(root + "\\"):
                # Allow shredding under user profile even if nested oddly
                if "\\users\\" in s and "\\appdata\\local\\temp" in s:
                    return False
                if s.startswith(windir + "\\"):
                    return True
                if s.startswith(pf + "\\") or s.startswith(pf86 + "\\"):
                    # Allow Desktop Toolkit install folder itself only for user files? safer: block PF
                    return True
    else:
        for root in ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/System", "/Applications"):
            if s == root.lower() or s.startswith(root.lower() + "/"):
                if root == "/Applications" and "desktoptoolkit" in s:
                    return False
                if root in ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/System"):
                    return True
    return False


def collect_targets(paths: list[str | Path]) -> tuple[list[Path], list[str]]:
    """Expand dirs to file list; return (files_and_empty_dirs_bottom_up hint paths, errors)."""
    files: list[Path] = []
    errors: list[str] = []
    seen: set[str] = set()

    def add_file(p: Path) -> None:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            return
        seen.add(key)
        files.append(p)

    for raw in paths:
        p = Path(raw)
        try:
            if not p.exists():
                errors.append(f"不存在：{p}")
                continue
            if _is_protected_path(p):
                errors.append(f"已跳过受保护路径：{p}")
                continue
            if p.is_symlink():
                # Shred the link node only (do not follow into target tree blindly)
                add_file(p)
                continue
            if p.is_file():
                add_file(p)
            elif p.is_dir():
                # topdown=False → files then their parent dirs (bottom-up)
                for root, dirnames, filenames in os.walk(p, topdown=False):
                    rp = Path(root)
                    if _is_protected_path(rp):
                        errors.append(f"已跳过受保护目录：{rp}")
                        dirnames[:] = []
                        continue
                    for name in filenames:
                        fp = rp / name
                        try:
                            if fp.is_symlink() or fp.is_file():
                                if _is_protected_path(fp):
                                    errors.append(f"已跳过受保护路径：{fp}")
                                else:
                                    add_file(fp)
                        except Exception as exc:
                            errors.append(f"{fp}: {exc}")
                    add_file(rp)
            else:
                errors.append(f"不支持的类型：{p}")
        except Exception as exc:
            errors.append(f"{p}: {exc}")
    return files, errors


def estimate_bytes(paths: list[Path]) -> int:
    total = 0
    for p in paths:
        try:
            if p.is_file() and not p.is_symlink():
                total += max(0, p.stat().st_size)
        except Exception:
            pass
    return total


def _make_writable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def _overwrite_file(path: Path, passes: int, *, cancel: Callable[[], bool] | None = None) -> None:
    size = path.stat().st_size
    if size <= 0:
        # Still open+truncate for empty files
        with open(path, "r+b", buffering=0) as f:
            f.truncate(0)
            f.flush()
            os.fsync(f.fileno())
        return

    patterns: list[bytes | None] = []
    # Eraser-like DoD-ish pattern set for first 3, then random
    base = [b"\x00", b"\xff", None]  # None = random
    for i in range(passes):
        if i < len(base):
            patterns.append(base[i])
        else:
            patterns.append(None)

    with open(path, "r+b", buffering=0) as f:
        for pi, pat in enumerate(patterns):
            if cancel and cancel():
                raise RuntimeError("已取消")
            f.seek(0)
            remaining = size
            while remaining > 0:
                if cancel and cancel():
                    raise RuntimeError("已取消")
                n = min(CHUNK, remaining)
                if pat is None:
                    buf = secrets.token_bytes(n)
                else:
                    buf = pat * n
                f.write(buf)
                remaining -= n
            f.flush()
            os.fsync(f.fileno())
        f.truncate(0)
        f.flush()
        os.fsync(f.fileno())


def _random_name(parent: Path, length: int = 12) -> Path:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    while True:
        name = "".join(secrets.choice(alphabet) for _ in range(length))
        cand = parent / name
        if not cand.exists():
            return cand


def _rename_scatter(path: Path) -> Path:
    """Rename to random short name in the same folder (harder name recovery)."""
    parent = path.parent
    cur = path
    for _ in range(2):
        dest = _random_name(parent)
        try:
            cur.rename(dest)
            cur = dest
        except Exception:
            break
    return cur


def shred_path(
    path: Path,
    *,
    passes: int = DEFAULT_PASSES,
    rename: bool = True,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """Shred a single file, or remove an empty directory. Raises on failure."""
    passes = clamp_passes(passes)
    if cancel and cancel():
        raise RuntimeError("已取消")
    if _is_protected_path(path):
        raise RuntimeError(f"受保护路径：{path}")
    if not path.exists():
        raise FileNotFoundError(str(path))

    if path.is_dir() and not path.is_symlink():
        # Expect children already shredded; only rmdir
        try:
            path.rmdir()
        except OSError as exc:
            # Non-empty: try again after ignoring leftovers best-effort
            raise RuntimeError(f"无法删除目录（可能非空）：{path} ({exc})") from exc
        return

    _make_writable(path)
    if path.is_symlink():
        path.unlink()
        return

    _overwrite_file(path, passes, cancel=cancel)
    cur = path
    if rename:
        cur = _rename_scatter(path)
    try:
        cur.unlink()
    except Exception:
        _make_writable(cur)
        cur.unlink()


def shred_many(
    paths: list[str | Path],
    *,
    passes: int = DEFAULT_PASSES,
    rename: bool = True,
    progress: ProgressCb | None = None,
    log: LogCb | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ShredReport:
    """Shred files/folders. Directories expanded; folder nodes removed after files."""
    passes = clamp_passes(passes)
    report = ShredReport()
    targets, prep_errors = collect_targets(list(paths))
    for err in prep_errors:
        if log:
            log(err)
        report.results.append(ShredItemResult(path=err, ok=False, detail=err))

    # Order: files first (os.walk already bottom-up for dirs appended after files in walk,
    # but collect_targets appends dir after its files). Keep as-is.
    total = max(1, len(targets))
    for i, path in enumerate(targets):
        if cancel and cancel():
            report.cancelled = True
            if progress:
                progress("已取消", i / total)
            break
        label = str(path)
        if progress:
            progress(f"粉碎中 ({i + 1}/{total})：{path.name}", i / total)
        if log:
            log(f"开始：{label}")
        try:
            if path.is_dir() and not path.is_symlink():
                # Remove directory after children; if still has entries, fail clearly
                shred_path(path, passes=passes, rename=False, cancel=cancel)
            else:
                shred_path(path, passes=passes, rename=rename, cancel=cancel)
            report.results.append(ShredItemResult(path=label, ok=True, detail="已粉碎"))
            if log:
                log(f"完成：{label}")
        except Exception as exc:
            report.results.append(ShredItemResult(path=label, ok=False, detail=str(exc)))
            if log:
                log(f"失败：{label} — {exc}")
    if progress and not report.cancelled:
        progress(report.summary(), 1.0)
    return report


# ---------------------------------------------------------------------------
# Windows Explorer context-menu registration (per-user, no admin)
# ---------------------------------------------------------------------------

SHELL_VERB = "DesktopToolkitShred"
SHELL_LABEL = "用 Desktop Toolkit 粉碎"


def _exe_for_shell() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    # Dev: launch via pythonw/python -m style pointing at main.py
    return Path(sys.executable)


def _command_template(exe: Path) -> str:
    if getattr(sys, "frozen", False):
        return f'"{exe}" --shred "%1"'
    main_py = Path(__file__).resolve().parent / "main.py"
    return f'"{exe}" "{main_py}" --shred "%1"'


def shell_context_registered() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\*\shell\{SHELL_VERB}",
        )
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def register_shell_context(*, exe: Path | None = None) -> None:
    """Add per-user right-click verb for files and folders."""
    if sys.platform != "win32":
        raise RuntimeError("资源管理器右键菜单仅支持 Windows")
    import winreg

    exe_path = exe or _exe_for_shell()
    cmd = _command_template(exe_path)
    icon = str(exe_path)

    def _write(root_sub: str) -> None:
        base = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{root_sub}\shell\{SHELL_VERB}",
        )
        winreg.SetValueEx(base, None, 0, winreg.REG_SZ, SHELL_LABEL)
        winreg.SetValueEx(base, "Icon", 0, winreg.REG_SZ, icon)
        # Show toward top of menu when possible
        winreg.SetValueEx(base, "Position", 0, winreg.REG_SZ, "Top")
        cmd_key = winreg.CreateKey(base, "command")
        winreg.SetValueEx(cmd_key, None, 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(cmd_key)
        winreg.CloseKey(base)

    _write("*")
    _write("Directory")


def unregister_shell_context() -> None:
    if sys.platform != "win32":
        return
    import winreg

    def _delete_tree(root_sub: str) -> None:
        path = rf"Software\Classes\{root_sub}\shell\{SHELL_VERB}"
        try:
            # delete command subkey first
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path + r"\command")
            except OSError:
                pass
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    for sub in ("*", "Directory", "AllFilesystemObjects"):
        _delete_tree(sub)
