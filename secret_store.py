"""Protect sensitive strings at rest (Windows DPAPI; elsewhere Fernet + local key).

In-memory app state stays plaintext; only values written to state.json are wrapped.
Legacy plaintext values are accepted on read and re-encrypted on the next save.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

_PREFIX = "enc:v1:"


def _fernet_key_path() -> Path:
    # Inline path (avoid circular import with storage.py)
    configured = os.environ.get("DESKTOP_TOOLKIT_DATA_DIR", "")
    if configured:
        base = Path(configured)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "DesktopToolkit"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "DesktopToolkit"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "DesktopToolkit"
    return base / ".secret_key"


def _fernet():
    from cryptography.fernet import Fernet

    path = _fernet_key_path()
    if path.is_file():
        key = path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            if sys.platform != "win32":
                os.chmod(path, 0o600)
        except OSError:
            pass
    return Fernet(key)


def protect(plaintext: str) -> str:
    """Return an opaque token safe to store in JSON, or empty string."""
    text = plaintext or ""
    if not text:
        return ""
    if text.startswith(_PREFIX):
        return text
    raw = text.encode("utf-8")
    try:
        if sys.platform == "win32":
            import win32crypt

            blob = win32crypt.CryptProtectData(raw, None, None, None, None, 0)
            return _PREFIX + "dpapi:" + base64.b64encode(blob).decode("ascii")
        token = _fernet().encrypt(raw)
        return _PREFIX + "fernet:" + token.decode("ascii")
    except Exception:
        # Fail closed for new writes only if caller checks; keep best-effort
        return text


def unprotect(value: str) -> str:
    """Decode a protected token; pass through legacy plaintext unchanged."""
    text = value or ""
    if not text.startswith(_PREFIX):
        return text
    body = text[len(_PREFIX) :]
    try:
        if body.startswith("dpapi:"):
            if sys.platform != "win32":
                return ""
            import win32crypt

            blob = base64.b64decode(body[6:].encode("ascii"))
            _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
            return data.decode("utf-8", errors="replace")
        if body.startswith("fernet:"):
            token = body[7:].encode("ascii")
            return _fernet().decrypt(token).decode("utf-8", errors="replace")
    except Exception:
        return ""
    return ""


# Known sensitive leaves in state.json (relative dotted / list paths handled in walk)
_SECRET_KEYS = frozenset(
    {
        "owm_api_key",
        "cf_api_token",
        "host_password",
        "client_password",
        "password",
    }
)


def _looks_secret_key(key: str) -> bool:
    if key in _SECRET_KEYS:
        return True
    low = key.lower()
    return low.endswith("_password") or low.endswith("_api_token") or low.endswith("_api_key")


def protect_state(state: dict) -> dict:
    """Deep-copy *state* and encrypt known secret string fields for disk."""
    import copy

    out = copy.deepcopy(state)

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and _looks_secret_key(str(k)):
                    node[k] = protect(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out


def unprotect_state(state: dict) -> dict:
    """Decrypt known secret fields in-place (mutates and returns *state*)."""

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and _looks_secret_key(str(k)):
                    node[k] = unprotect(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(state)
    return state
