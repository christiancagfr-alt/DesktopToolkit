"""Local JSON state for Desktop Toolkit (no pet data required)."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

def _default_float_assistant() -> bool:
    # Show floating robot on Windows / macOS / Linux by default.
    return True


DEFAULT_STATE: dict[str, Any] = {
    "app_name": "DesktopToolkit",
    "prefs": {
        "theme": "dark",
        "autostart": False,
        "float_assistant": _default_float_assistant(),
        "tips_enabled": True,
        "voice_enabled": True,
    },
    "todo_board": {"color": "#fef08a"},
    "screenshot": {
        "save_dir": "",
        "auto_upload": False,
        "hotkey_region": "Ctrl+Alt+A",
        "hotkey_full": "Ctrl+Alt+Shift+A",
        "gdrive": {
            "enabled": False,
            "client_secrets_path": "",
            "folder_id": "",
            "folder_name": "ToolkitShots",
            "use_full_scope": False,
        },
    },
    "recorder": {},
    "cleaner": {"speak": False, "bubble": True, "keep_log": True, "scopes": []},
    "p2p": {},
    "todos": [],
    "notes": [],
    "alarms": [],
    "timer": {},
    "pomodoro": {
        "work_min": 25,
        "break_min": 5,
        "long_break_min": 15,
        "cycles_before_long": 4,
        "phase": "idle",
        "ends_at": None,
        "cycle_count": 0,
        "paused": False,
        "remaining_sec": 0,
    },
    "music": {},
    "lan": {},
    "weather": {
        "enabled": False,
        "provider": "open-meteo",  # open-meteo (default) | openweathermap
        "location_mode": "auto",  # auto | manual | coords
        "location_text": "",
        "last_place": "",
        "latitude": "",
        "longitude": "",
        "owm_api_key": "",
        "interval_min": 60,  # 0 = only manual
        "announce_on_start": False,
    },
}

_WRITE_LOCK = threading.RLock()


def app_data_dir(app_name: str = "DesktopToolkit") -> Path:
    """Platform-correct writable config directory."""
    configured = os.environ.get("DESKTOP_TOOLKIT_DATA_DIR", "")
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / app_name


class JsonStore:
    def __init__(self, app_name: str = "DesktopToolkit") -> None:
        preferred = app_data_dir(app_name)
        self.directory = self._select_writable(preferred, app_name)
        self.state_path = self.directory / "state.json"
        self.log_path = self.directory / "log.json"
        self.state = self._load_state()

    @staticmethod
    def _select_writable(preferred: Path, app_name: str) -> Path:
        candidates = [preferred]
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            candidates.append(Path(local) / app_name)
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / f"{app_name}Data")
        else:
            candidates.append(Path(__file__).resolve().parent / f"{app_name}Data")
        tmp = os.environ.get("TEMP") or str(Path.home())
        candidates.append(Path(tmp) / app_name)
        seen: set[str] = set()
        for c in candidates:
            k = str(c).lower()
            if k in seen:
                continue
            seen.add(k)
            try:
                c.mkdir(parents=True, exist_ok=True)
                probe = c / f".w{os.getpid()}"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return c
            except OSError:
                continue
        import tempfile

        return Path(tempfile.mkdtemp(prefix=f"{app_name}-"))

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return copy.deepcopy(DEFAULT_STATE)
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return copy.deepcopy(DEFAULT_STATE)
        merged = self._merge(saved, DEFAULT_STATE)
        try:
            from secret_store import unprotect_state

            unprotect_state(merged)
        except Exception:
            pass
        return merged

    @staticmethod
    def _merge(value: dict, defaults: dict) -> dict:
        merged = copy.deepcopy(defaults)
        for k, item in value.items():
            if isinstance(item, dict) and isinstance(merged.get(k), dict):
                merged[k] = JsonStore._merge(item, merged[k])
            else:
                merged[k] = item
        return merged

    def save_state(self) -> None:
        with _WRITE_LOCK:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            try:
                from secret_store import protect_state

                payload = protect_state(self.state)
            except Exception:
                payload = self.state
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    def append_log(self, event_type: str, message: str, **details: Any) -> None:
        records: list = []
        if self.log_path.exists():
            try:
                records = json.loads(self.log_path.read_text(encoding="utf-8"))
            except Exception:
                records = []
        if not isinstance(records, list):
            records = []
        records.append(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "type": event_type,
                "message": message,
                "details": details,
            }
        )
        records = records[-500:]
        try:
            self.log_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def read_log(self) -> list:
        if not self.log_path.exists():
            return []
        try:
            data = json.loads(self.log_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
