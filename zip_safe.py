"""Zip extraction helpers that reject path-traversal (Zip Slip) members."""

from __future__ import annotations

import zipfile
import shutil
from pathlib import Path

MAX_MEMBERS = 10_000
MAX_MEMBER_SIZE = 4 * 1024 * 1024 * 1024
MAX_TOTAL_SIZE = 20 * 1024 * 1024 * 1024


def safe_extractall(
    zf: zipfile.ZipFile,
    dest: Path,
    *,
    max_members: int = MAX_MEMBERS,
    max_member_size: int = MAX_MEMBER_SIZE,
    max_total_size: int = MAX_TOTAL_SIZE,
) -> None:
    """Extract *zf* into *dest*, refusing members that escape the destination.

    Raises ValueError if any archive member would write outside *dest*
    (classic Zip Slip via ``../`` or absolute paths).
    """
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    members = zf.infolist()
    if len(members) > max_members:
        raise ValueError(f"拒绝解压：文件数量超过限制（{max_members}）")
    total_size = 0
    for info in members:
        if info.file_size < 0 or info.file_size > max_member_size:
            raise ValueError(f"拒绝解压超大文件：{info.filename}")
        total_size += info.file_size
        if total_size > max_total_size:
            raise ValueError("拒绝解压：压缩包展开后的总大小超过限制")
    free = shutil.disk_usage(dest).free
    if total_size > max(0, free - 128 * 1024 * 1024):
        raise ValueError("磁盘剩余空间不足，已停止解压")

    written_total = 0
    for info in members:
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
            written_member = 0
            while True:
                chunk = src.read(1024 * 256)
                if not chunk:
                    break
                written_member += len(chunk)
                written_total += len(chunk)
                if written_member > info.file_size or written_total > total_size:
                    raise ValueError("压缩包实际展开大小与目录信息不符")
                out.write(chunk)
