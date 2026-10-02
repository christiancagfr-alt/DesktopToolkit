"""Global hotkeys for Desktop Toolkit (screenshot + open hub).

Windows: RegisterHotKey (system-wide even when app unfocused).
macOS / Linux: pynput GlobalHotKeys (true global; macOS needs Accessibility /
  Input Monitoring permission for the app).
"""

from __future__ import annotations

import sys
from typing import Callable

from PyQt6.QtCore import QAbstractNativeEventFilter, QTimer, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QWidget

WM_HOTKEY = 0x0312
HOTKEY_HUB = 0xD001
HOTKEY_SHOT_REGION = 0xD002
HOTKEY_SHOT_FULL = 0xD003
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004


def _user32():
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.user32


def parse_hotkey_combo(combo: str) -> tuple[int, int] | None:
    if not combo or not str(combo).strip():
        return None
    parts = [p.strip().upper() for p in str(combo).replace(" ", "").split("+") if p.strip()]
    mods = 0
    vk: int | None = None
    for p in parts:
        if p in ("CTRL", "CONTROL", "CONTROLKEY", "⌃"):
            mods |= MOD_CONTROL
        elif p in ("ALT", "OPTION", "OPT", "⌥"):
            mods |= MOD_ALT
        elif p == "SHIFT":
            mods |= MOD_SHIFT
        elif p in ("WIN", "META", "CMD", "COMMAND", "⌘"):
            mods |= 0x0008
        elif len(p) == 1 and ("A" <= p <= "Z" or "0" <= p <= "9"):
            vk = ord(p)
        elif p.startswith("F") and p[1:].isdigit():
            n = int(p[1:])
            if 1 <= n <= 24:
                vk = 0x70 + n - 1
        elif p in ("PRINTSCREEN", "PRTSC"):
            vk = 0x2C
        elif p == "SPACE":
            vk = 0x20
    if vk is None:
        return None
    return mods, vk


def _normalize_qt_combo(combo: str) -> str:
    """Map friendly names to QKeySequence text (Cmd/Option on Mac)."""
    s = (combo or "").strip()
    if not s:
        return s
    parts = [p.strip() for p in s.replace(" ", "").split("+") if p.strip()]
    mapped: list[str] = []
    for p in parts:
        up = p.upper()
        if up in ("COMMAND", "CMD") or p == "⌘":
            mapped.append("Meta")
        elif up in ("OPTION", "OPT") or p == "⌥":
            mapped.append("Alt")
        elif up in ("CONTROL", "CTRL") or p == "⌃":
            mapped.append("Ctrl")
        elif up == "SHIFT":
            mapped.append("Shift")
        elif up in ("WIN", "META"):
            mapped.append("Meta")
        else:
            mapped.append(p)
    return "+".join(mapped)


def _to_pynput_hotkey(combo: str) -> str | None:
    """Convert 'Ctrl+Alt+A' → '<ctrl>+<alt>+a' for pynput.GlobalHotKeys."""
    s = (combo or "").strip()
    if not s:
        return None
    parts = [p.strip() for p in s.replace(" ", "").split("+") if p.strip()]
    out: list[str] = []
    key: str | None = None
    for p in parts:
        up = p.upper()
        if up in ("CTRL", "CONTROL") or p == "⌃":
            out.append("<ctrl>")
        elif up in ("ALT", "OPTION", "OPT") or p == "⌥":
            out.append("<alt>")
        elif up == "SHIFT":
            out.append("<shift>")
        elif up in ("CMD", "COMMAND", "META", "WIN") or p == "⌘":
            out.append("<cmd>" if sys.platform == "darwin" else "<cmd>")
        elif up == "SPACE":
            key = "<space>"
        elif up.startswith("F") and up[1:].isdigit():
            key = f"<{up.lower()}>"
        elif len(p) == 1:
            key = p.lower()
        else:
            return None
    if not key:
        return None
    out.append(key)
    return "+".join(out)


class _Filter(QAbstractNativeEventFilter):
    def __init__(self, handlers: dict[int, Callable[[], None]]) -> None:
        super().__init__()
        self.handlers = handlers

    def nativeEventFilter(self, event_type, message):
        if event_type != b"windows_generic_MSG":
            return False, 0
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == WM_HOTKEY:
            cb = self.handlers.get(int(msg.wParam))
            if cb:
                QTimer.singleShot(0, cb)
                return True, 0
        return False, 0


class ToolkitHotkeys:
    def __init__(
        self,
        *,
        open_hub: Callable[[], None],
        shot_region: Callable[[], None],
        shot_full: Callable[[], None],
        hub_combo: str = "Ctrl+Alt+T",
        region_combo: str = "Ctrl+Alt+A",
        full_combo: str = "Ctrl+Alt+Shift+A",
    ) -> None:
        self.open_hub = open_hub
        self.shot_region = shot_region
        self.shot_full = shot_full
        self.hub_combo = hub_combo
        self.region_combo = region_combo
        self.full_combo = full_combo
        self._ids: list[int] = []
        self._handlers = {
            HOTKEY_HUB: open_hub,
            HOTKEY_SHOT_REGION: shot_region,
            HOTKEY_SHOT_FULL: shot_full,
        }
        self._filter = None
        self._qt_shortcuts: list[QShortcut] = []
        self._qt_host: QWidget | None = None
        self._qt_shot_map: list[tuple[QShortcut, int]] = []
        self._pynput_listener = None
        self._shots_paused = False
        self._use_win = sys.platform == "win32" and _user32() is not None
        self._use_pynput = not self._use_win

        if self._use_win:
            self._filter = _Filter(self._handlers)
            app = QApplication.instance()
            if app:
                app.installNativeEventFilter(self._filter)
            self._register_all()
        else:
            self._register_global_unix()

    # ---- Windows RegisterHotKey ----
    def _unregister(self) -> None:
        u = _user32()
        if not u:
            return
        for hid in list(self._ids):
            try:
                u.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._ids.clear()

    def _unregister_shots(self) -> None:
        if self._use_pynput:
            self._shots_paused = True
            self._register_global_unix(include_shots=False)
            return
        if not self._use_win:
            for sc, _hid in getattr(self, "_qt_shot_map", []):
                try:
                    sc.setEnabled(False)
                except Exception:
                    pass
            return
        u = _user32()
        if not u:
            return
        for hid in (HOTKEY_SHOT_REGION, HOTKEY_SHOT_FULL):
            try:
                u.UnregisterHotKey(None, hid)
            except Exception:
                pass
            if hid in self._ids:
                self._ids.remove(hid)

    def _register_all(self) -> None:
        u = _user32()
        if not u:
            return
        self._unregister()
        import ctypes
        from ctypes import wintypes

        try:
            u.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            u.RegisterHotKey.restype = wintypes.BOOL
        except Exception:
            pass
        for hid, combo, default in (
            (HOTKEY_HUB, self.hub_combo, (MOD_CONTROL | MOD_ALT, ord("T"))),
            (HOTKEY_SHOT_REGION, self.region_combo, (MOD_CONTROL | MOD_ALT, ord("A"))),
            (HOTKEY_SHOT_FULL, self.full_combo, (MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("A"))),
        ):
            parsed = parse_hotkey_combo(combo) or default
            mods, vk = parsed
            if u.RegisterHotKey(None, hid, int(mods), int(vk)):
                self._ids.append(hid)

    def _register_shots_only(self) -> None:
        u = _user32()
        if not u:
            return
        self._unregister_shots()
        import ctypes
        from ctypes import wintypes

        try:
            u.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            u.RegisterHotKey.restype = wintypes.BOOL
        except Exception:
            pass
        for hid, combo, default in (
            (HOTKEY_SHOT_REGION, self.region_combo, (MOD_CONTROL | MOD_ALT, ord("A"))),
            (HOTKEY_SHOT_FULL, self.full_combo, (MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("A"))),
        ):
            parsed = parse_hotkey_combo(combo) or default
            mods, vk = parsed
            if u.RegisterHotKey(None, hid, int(mods), int(vk)):
                self._ids.append(hid)

    # ---- macOS / Linux: pynput global hotkeys ----
    def _qt_invoke(self, fn: Callable[[], None]) -> Callable[[], None]:
        def _wrap() -> None:
            QTimer.singleShot(0, fn)

        return _wrap

    def _stop_pynput(self) -> None:
        listener = self._pynput_listener
        self._pynput_listener = None
        if listener is None:
            return
        try:
            listener.stop()
        except Exception:
            pass

    def _register_global_unix(self, *, include_shots: bool = True) -> None:
        """Bind system-wide hotkeys via pynput (works while other apps are focused)."""
        self._stop_pynput()
        self._ids.clear()
        mapping: dict[str, Callable[[], None]] = {}

        def _add(combo: str, hid: int, handler: Callable[[], None]) -> bool:
            key = _to_pynput_hotkey(combo)
            if not key:
                return False
            mapping[key] = self._qt_invoke(handler)
            if hid not in self._ids:
                self._ids.append(hid)
            return True

        ok_h = _add(self.hub_combo or "Ctrl+Alt+T", HOTKEY_HUB, self.open_hub)
        ok_r = ok_f = False
        if include_shots and not self._shots_paused:
            ok_r = _add(
                self.region_combo or "Ctrl+Alt+A",
                HOTKEY_SHOT_REGION,
                self.shot_region,
            )
            ok_f = _add(
                self.full_combo or "Ctrl+Alt+Shift+A",
                HOTKEY_SHOT_FULL,
                self.shot_full,
            )

        if not mapping:
            # Fallback: in-app Qt shortcuts if pynput mapping failed entirely
            self._register_qt_all()
            return

        try:
            from pynput import keyboard

            listener = keyboard.GlobalHotKeys(mapping)
            listener.start()
            self._pynput_listener = listener
        except Exception:
            # Fall back to ApplicationShortcut (only while Toolkit is focused)
            self._register_qt_all()
            return

        # Keep Qt ids for status reporting
        if ok_h and HOTKEY_HUB not in self._ids:
            self._ids.append(HOTKEY_HUB)
        if ok_r and HOTKEY_SHOT_REGION not in self._ids:
            self._ids.append(HOTKEY_SHOT_REGION)
        if ok_f and HOTKEY_SHOT_FULL not in self._ids:
            self._ids.append(HOTKEY_SHOT_FULL)

    # ---- Qt ApplicationShortcut fallback ----
    def _ensure_qt_host(self) -> QWidget | None:
        app = QApplication.instance()
        if app is None:
            return None
        host = getattr(app, "_toolkit_hotkey_host", None)
        if host is None:
            host = QWidget()
            host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            host.hide()
            app._toolkit_hotkey_host = host  # type: ignore[attr-defined]
        self._qt_host = host
        return host

    def _clear_qt(self) -> None:
        for sc in self._qt_shortcuts:
            try:
                sc.setParent(None)
                sc.deleteLater()
            except Exception:
                pass
        self._qt_shortcuts.clear()
        self._qt_shot_map = []
        self._ids.clear()

    def _bind_qt(self, combo: str, handler: Callable[[], None], hid: int, *, shot: bool = False) -> bool:
        host = self._ensure_qt_host()
        if host is None:
            return False
        seq_txt = _normalize_qt_combo(combo)
        seq = QKeySequence(seq_txt)
        if seq.isEmpty():
            return False
        sc = QShortcut(seq, host)
        sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc.setEnabled(True)
        sc.activated.connect(handler)
        self._qt_shortcuts.append(sc)
        self._ids.append(hid)
        if shot:
            self._qt_shot_map.append((sc, hid))
        return True

    def _register_qt_all(self) -> None:
        self._clear_qt()
        self._qt_shot_map = []
        self._bind_qt(self.hub_combo or "Ctrl+Alt+T", self.open_hub, HOTKEY_HUB)
        self._bind_qt(
            self.region_combo or "Ctrl+Alt+A", self.shot_region, HOTKEY_SHOT_REGION, shot=True
        )
        self._bind_qt(
            self.full_combo or "Ctrl+Alt+Shift+A", self.shot_full, HOTKEY_SHOT_FULL, shot=True
        )

    def pause_screenshot_hotkeys(self) -> None:
        """Free shot combos so capture widgets can receive those keys."""
        self._unregister_shots()

    def resume_screenshot_hotkeys(self) -> None:
        if self._use_pynput:
            self._shots_paused = False
            self._register_global_unix(include_shots=True)
            return
        if not self._use_win:
            for sc, _hid in getattr(self, "_qt_shot_map", []):
                try:
                    sc.setEnabled(True)
                except Exception:
                    pass
            for hid in (HOTKEY_SHOT_REGION, HOTKEY_SHOT_FULL):
                if hid not in self._ids:
                    self._ids.append(hid)
            return
        self._register_shots_only()

    def rebind(self, hub: str | None = None, region: str | None = None, full: str | None = None) -> str:
        if hub is not None:
            self.hub_combo = hub
        if region is not None:
            self.region_combo = region
        if full is not None:
            self.full_combo = full
        if self._use_win:
            self._register_all()
        elif self._use_pynput:
            self._register_global_unix(include_shots=not self._shots_paused)
        else:
            self._register_qt_all()
        r_ok = HOTKEY_SHOT_REGION in self._ids
        f_ok = HOTKEY_SHOT_FULL in self._ids
        h_ok = HOTKEY_HUB in self._ids
        if not self._use_win:
            plat = "macOS" if sys.platform == "darwin" else "Linux"
            note = "已保存（全局快捷键，其它应用前台时也可用"
            if sys.platform == "darwin":
                note += "；若无效请到「系统设置→隐私与安全→辅助功能」和「输入监控」允许本应用"
            note += "）"
            status = "OK" if (r_ok and f_ok and h_ok) else "部分生效"
            return (
                f"{plat} 快捷键{status} · 面板 {self.hub_combo} · "
                f"区域 {self.region_combo} · 全屏 {self.full_combo} · {note}"
            )
        return (
            f"面板 {self.hub_combo}:{'OK' if h_ok else '失败'} · "
            f"区域 {self.region_combo}:{'OK' if r_ok else '失败'} · "
            f"全屏 {self.full_combo}:{'OK' if f_ok else '失败'}"
        )

    def close(self) -> None:
        if self._use_win:
            self._unregister()
        else:
            self._stop_pynput()
            self._clear_qt()
