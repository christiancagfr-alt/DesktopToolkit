"""List / uninstall Windows programs and clean leftover Uninstall registry keys.

Windows-only. Non-Windows callers get empty lists / clear errors.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class InstalledApp:
    display_name: str
    publisher: str = ""
    version: str = ""
    uninstall_string: str = ""
    quiet_uninstall_string: str = ""
    install_location: str = ""
    display_icon: str = ""  # raw DisplayIcon from registry (path[,index])
    key_name: str = ""
    hive: str = ""  # HKLM / HKCU
    wow64: bool = False
    estimated_size_kb: int = 0
    system_component: bool = False

    @property
    def source_label(self) -> str:
        bit = "32-bit" if self.wow64 else "64-bit"
        return f"{self.hive} · {bit}"


def parse_display_icon(raw: str) -> tuple[str, int]:
    """Split registry DisplayIcon into (path, icon_index)."""
    text = (raw or "").strip().strip('"')
    if not text:
        return "", 0
    # Common forms: C:\App\app.exe | C:\App\app.exe,0 | "C:\App\app.exe",0
    if "," in text:
        path, _, idx = text.rpartition(",")
        path = path.strip().strip('"')
        try:
            return path, int(idx.strip())
        except ValueError:
            return text.strip('"'), 0
    return text, 0


def icon_for_app(app: InstalledApp):
    """Return a QIcon for *app* (Windows shell icons from DisplayIcon / exe)."""
    from PyQt6.QtCore import QFileInfo
    from PyQt6.QtWidgets import QFileIconProvider

    candidates: list[str] = []
    if app.display_icon:
        path, _index = parse_display_icon(app.display_icon)
        if path:
            candidates.append(path)
    for raw in (app.uninstall_string, app.quiet_uninstall_string):
        parts = _split_command(raw)
        if parts:
            candidates.append(os.path.expandvars(parts[0]))
    if app.install_location:
        loc = Path(os.path.expandvars(app.install_location))
        if loc.is_dir():
            for pattern in ("*.exe",):
                found = sorted(loc.glob(pattern))
                if found:
                    candidates.append(str(found[0]))
                    break

    provider = QFileIconProvider()
    seen: set[str] = set()
    for path in candidates:
        if not path:
            continue
        expanded = os.path.expandvars(path)
        key = expanded.lower()
        if key in seen:
            continue
        seen.add(key)
        p = Path(expanded)
        if not p.is_file():
            continue
        icon = provider.icon(QFileInfo(str(p)))
        if not icon.isNull():
            return icon
    # Generic fallback (PyQt6 has no Application IconType)
    for fallback in (
        r"C:\Windows\System32\shell32.dll",
        r"C:\Windows\explorer.exe",
    ):
        if Path(fallback).is_file():
            icon = provider.icon(QFileInfo(fallback))
            if not icon.isNull():
                return icon
    return provider.icon(QFileIconProvider.IconType.File)


@dataclass
class CleanupReport:
    removed_keys: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def supported() -> bool:
    return sys.platform.startswith("win")


def _winreg():
    import winreg

    return winreg


def _uninstall_roots():
    """Yield (hive_label, hive_const, subkey, wow64)."""
    winreg = _winreg()
    roots = [
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", False),
        ("HKCU", winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", False),
    ]
    if hasattr(winreg, "KEY_WOW64_32KEY"):
        roots.append(
            (
                "HKLM",
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                True,
            )
        )
        roots.append(
            (
                "HKCU",
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                True,
            )
        )
    return roots


def _open_key(hive, subkey: str, *, wow64: bool, access):
    winreg = _winreg()
    sam = access
    if wow64 and hasattr(winreg, "KEY_WOW64_32KEY"):
        sam |= winreg.KEY_WOW64_32KEY
    elif (not wow64) and hasattr(winreg, "KEY_WOW64_64KEY") and hive == winreg.HKEY_LOCAL_MACHINE:
        # Prefer native view on 64-bit Windows
        try:
            sam |= winreg.KEY_WOW64_64KEY
        except Exception:
            pass
    return winreg.OpenKey(hive, subkey, 0, sam)


def _read_str(key, name: str) -> str:
    winreg = _winreg()
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return str(val or "").strip()
    except OSError:
        return ""


def _read_int(key, name: str) -> int:
    winreg = _winreg()
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return int(val or 0)
    except (OSError, TypeError, ValueError):
        return 0


def list_installed_apps(*, include_system: bool = False) -> list[InstalledApp]:
    if not supported():
        return []
    winreg = _winreg()
    seen: set[tuple[str, str, str]] = set()
    apps: list[InstalledApp] = []

    for hive_label, hive, subkey, wow64 in _uninstall_roots():
        try:
            root = _open_key(hive, subkey, wow64=wow64, access=winreg.KEY_READ)
        except OSError:
            continue
        try:
            n_sub, _, _ = winreg.QueryInfoKey(root)
            for i in range(n_sub):
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    continue
                try:
                    with winreg.OpenKey(root, name) as app_key:
                        display = _read_str(app_key, "DisplayName")
                        if not display:
                            continue
                        system = _read_int(app_key, "SystemComponent") == 1
                        parent = _read_str(app_key, "ParentKeyName")
                        release_type = _read_str(app_key, "ReleaseType").lower()
                        if not include_system and (system or parent or "update" in release_type):
                            continue
                        uninstall = _read_str(app_key, "UninstallString")
                        quiet = _read_str(app_key, "QuietUninstallString")
                        if not uninstall and not quiet:
                            continue
                        dedupe = (hive_label, name.lower(), display.lower())
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        apps.append(
                            InstalledApp(
                                display_name=display,
                                publisher=_read_str(app_key, "Publisher"),
                                version=_read_str(app_key, "DisplayVersion"),
                                uninstall_string=uninstall,
                                quiet_uninstall_string=quiet,
                                install_location=_read_str(app_key, "InstallLocation"),
                                display_icon=_read_str(app_key, "DisplayIcon"),
                                key_name=name,
                                hive=hive_label,
                                wow64=wow64,
                                estimated_size_kb=_read_int(app_key, "EstimatedSize"),
                                system_component=system,
                            )
                        )
                except OSError:
                    continue
        finally:
            winreg.CloseKey(root)

    apps.sort(key=lambda a: a.display_name.lower())
    return apps


def _split_command(cmdline: str) -> list[str]:
    text = (cmdline or "").strip()
    if not text:
        return []
    try:
        parts = shlex.split(text, posix=False)
    except ValueError:
        parts = [text]
    return [p for p in parts if p]


def _normalize_msiexec(argv: list[str]) -> list[str]:
    """Prefer /X{GUID} uninstall form when given /I{GUID}."""
    if not argv:
        return argv
    out = list(argv)
    low0 = out[0].lower()
    if "msiexec" not in low0:
        return out
    for i, part in enumerate(out[1:], start=1):
        m = re.match(r"^/I\{([0-9A-Fa-f-]+)\}$", part, re.I)
        if m:
            out[i] = f"/X{{{m.group(1)}}}"
            break
    return out


def run_uninstall(
    app: InstalledApp,
    *,
    prefer_quiet: bool = False,
    timeout_sec: float | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Launch the vendor uninstaller. Returns process exit code (or -1 on spawn failure)."""
    if not supported():
        raise RuntimeError("软件卸载仅支持 Windows")
    raw = ""
    if prefer_quiet and app.quiet_uninstall_string:
        raw = app.quiet_uninstall_string
    else:
        raw = app.uninstall_string or app.quiet_uninstall_string
    if not raw:
        raise RuntimeError("该程序没有可用的卸载命令")

    argv = _normalize_msiexec(_split_command(raw))
    if not argv:
        raise RuntimeError(f"无法解析卸载命令：{raw}")

    # Expand env vars in path-like first arg
    argv[0] = os.path.expandvars(argv[0])
    if log:
        log(f"执行：{' '.join(argv)}")

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(Path(argv[0]).parent) if Path(argv[0]).is_file() else None,
            shell=False,
        )
    except OSError as exc:
        # Some uninstall strings need shell (rare). Last resort without elevating blindly.
        if log:
            log(f"直接启动失败（{exc}），改用 shell 再试…")
        proc = subprocess.Popen(raw, shell=True)

    if timeout_sec is None:
        return int(proc.wait())
    try:
        return int(proc.wait(timeout=timeout_sec))
    except subprocess.TimeoutExpired:
        if log:
            log("卸载程序仍在运行（已超时等待）；请在弹出的卸载窗口中完成操作。")
        return -2


def _delete_uninstall_key(hive_label: str, key_name: str, *, wow64: bool) -> None:
    winreg = _winreg()
    hive = winreg.HKEY_LOCAL_MACHINE if hive_label == "HKLM" else winreg.HKEY_CURRENT_USER
    parent = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    access = winreg.KEY_ALL_ACCESS
    if wow64 and hasattr(winreg, "KEY_WOW64_32KEY"):
        access |= winreg.KEY_WOW64_32KEY
    elif (not wow64) and hasattr(winreg, "KEY_WOW64_64KEY") and hive_label == "HKLM":
        access |= winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(hive, parent, 0, access) as root:
        winreg.DeleteKey(root, key_name)


def _iter_matching_uninstall_keys(display_name: str, key_name: str):
    winreg = _winreg()
    target = (display_name or "").strip().lower()
    key_l = (key_name or "").strip().lower()
    for hive_label, hive, subkey, wow64 in _uninstall_roots():
        try:
            root = _open_key(hive, subkey, wow64=wow64, access=winreg.KEY_READ)
        except OSError:
            continue
        try:
            n_sub, _, _ = winreg.QueryInfoKey(root)
            for i in range(n_sub):
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    continue
                try:
                    with winreg.OpenKey(root, name) as app_key:
                        display = _read_str(app_key, "DisplayName")
                except OSError:
                    continue
                if name.lower() == key_l or (target and display.lower() == target):
                    yield hive_label, name, wow64, display
        finally:
            winreg.CloseKey(root)


def _safe_product_dir_name(text: str) -> str:
    cleaned = re.sub(r"[^\w\- .]+", "", text or "", flags=re.UNICODE).strip()
    return cleaned[:80]


def cleanup_after_uninstall(
    app: InstalledApp,
    *,
    wait_sec: float = 1.5,
    clean_software_keys: bool = True,
    log: Callable[[str], None] | None = None,
) -> CleanupReport:
    """Remove leftover Uninstall registry entries (and optional Software keys) after uninstall."""
    report = CleanupReport()
    if not supported():
        report.errors.append("仅 Windows 支持注册表清理")
        return report

    if wait_sec > 0:
        time.sleep(wait_sec)

    # 1) Uninstall keys still pointing at this product
    for hive_label, name, wow64, display in _iter_matching_uninstall_keys(
        app.display_name, app.key_name
    ):
        label = f"{hive_label}\\…\\Uninstall\\{name}"
        try:
            _delete_uninstall_key(hive_label, name, wow64=wow64)
            report.removed_keys.append(label)
            if log:
                log(f"已删除注册表项：{label} ({display})")
        except PermissionError as exc:
            msg = f"权限不足，无法删除 {label}：{exc}"
            report.errors.append(msg)
            if log:
                log(msg)
        except OSError as exc:
            # Key may already be gone
            if getattr(exc, "winerror", None) == 2:
                report.skipped.append(f"{label}（已不存在）")
            else:
                report.errors.append(f"删除失败 {label}：{exc}")
                if log:
                    log(f"删除失败 {label}：{exc}")

    # 2) Optional publisher/product keys under Software\
    if clean_software_keys:
        pub = _safe_product_dir_name(app.publisher)
        prod = _safe_product_dir_name(app.display_name)
        # Avoid nuking giant publishers with one shared key when product name equals publisher
        candidates: list[tuple[str, str]] = []
        if pub and prod and pub.lower() != prod.lower():
            candidates.append((rf"SOFTWARE\{pub}\{prod}", f"Software\\{pub}\\{prod}"))
            candidates.append((rf"SOFTWARE\WOW6432Node\{pub}\{prod}", f"Software\\WOW6432Node\\{pub}\\{prod}"))
        if prod:
            # Some apps register directly under HKCU\Software\<Product>
            candidates.append((rf"SOFTWARE\{prod}", f"Software\\{prod}"))

        winreg = _winreg()
        for hive_label, hive in (
            ("HKCU", winreg.HKEY_CURRENT_USER),
            ("HKLM", winreg.HKEY_LOCAL_MACHINE),
        ):
            for sub, pretty in candidates:
                # Only delete if the key has few values and no subkeys (leaf leftover)
                try:
                    with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
                        n_sub, n_val, _ = winreg.QueryInfoKey(k)
                    if n_sub > 0:
                        report.skipped.append(f"{hive_label}\\{pretty}（含有子项，已跳过）")
                        continue
                    if n_val > 40:
                        report.skipped.append(f"{hive_label}\\{pretty}（值过多，已跳过）")
                        continue
                except OSError:
                    continue
                try:
                    parent, _, leaf = sub.rpartition("\\")
                    access = winreg.KEY_ALL_ACCESS
                    with winreg.OpenKey(hive, parent, 0, access) as pk:
                        winreg.DeleteKey(pk, leaf)
                    report.removed_keys.append(f"{hive_label}\\{pretty}")
                    if log:
                        log(f"已删除：{hive_label}\\{pretty}")
                except PermissionError as exc:
                    report.errors.append(f"权限不足 {hive_label}\\{pretty}：{exc}")
                except OSError as exc:
                    report.skipped.append(f"{hive_label}\\{pretty}（{exc}）")

    if not report.removed_keys and not report.errors:
        report.notes.append("未发现残留的卸载注册表项（卸载程序可能已自行清理）。")
        if log:
            log(report.notes[-1])
    return report


def format_size_kb(kb: int) -> str:
    if kb <= 0:
        return "—"
    if kb < 1024:
        return f"{kb} KB"
    mb = kb / 1024.0
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024.0:.2f} GB"
