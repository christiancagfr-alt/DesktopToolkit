"""Zip extraction helpers that reject path-traversal (Zip Slip) members."""

from __future__ import annotations

import zipfile
from pathlib import Path


def safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract *zf* into *dest*, refusing members that escape the destination.

    Raises ValueError if any archive member would write outside *dest*
    (classic Zip Slip via ``../`` or absolute paths).
    """
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if not name or name.endswith("/"):
            # Directory entries are created as needed when files are written.
            target = (dest / name).resolve()
            try:
                target.relative_to(dest)
            except ValueError as exc:
                raise ValueError(f"拒绝危险压缩路径：{info.filename}") from exc
            target.mkdir(parents=True, exist_ok=True)
            continue
        target = (dest / name).resolve()
        try:
            target.relative_to(dest)
        except ValueError as exc:
            raise ValueError(f"拒绝危险压缩路径：{info.filename}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
