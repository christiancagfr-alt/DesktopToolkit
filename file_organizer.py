"""Organize files into subfolders by filename rules (Windows / macOS / Linux).

Modes:
  - prefix: strip trailing _<digits> then group by remaining stem
  - date:   group by YYYY.MM.DD / YYYY-MM-DD / YYYY_MM_DD in the name
  - ext:    group by extension (optionally friendly type folders)
  - custom: split by delimiter; take first N parts OR only the N-th part
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


_TRAILING_SEQ = re.compile(r"^(.+)_(\d+)$")
_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[.\-_](\d{2})[.\-_](\d{2})"),
)
_BAD_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

EXT_TYPE_MAP: dict[str, str] = {
    ".png": "图片",
    ".jpg": "图片",
    ".jpeg": "图片",
    ".gif": "图片",
    ".webp": "图片",
    ".bmp": "图片",
    ".tif": "图片",
    ".tiff": "图片",
    ".heic": "图片",
    ".mp4": "视频",
    ".mov": "视频",
    ".avi": "视频",
    ".mkv": "视频",
    ".webm": "视频",
    ".mp3": "音频",
    ".wav": "音频",
    ".flac": "音频",
    ".m4a": "音频",
    ".aac": "音频",
    ".pdf": "文档",
    ".doc": "文档",
    ".docx": "文档",
    ".xls": "文档",
    ".xlsx": "文档",
    ".ppt": "文档",
    ".pptx": "文档",
    ".txt": "文档",
    ".md": "文档",
    ".zip": "压缩包",
    ".7z": "压缩包",
    ".rar": "压缩包",
}


@dataclass
class OrganizeOptions:
    mode: str = "custom"  # custom | prefix | date | ext
    recursive: bool = False
    min_group_size: int = 2  # skip creating folder for singleton groups when >1 would apply
    friendly_types: bool = True  # for ext mode
    custom_sep: str = "-"
    custom_parts: int = 3
    # first = join first N segments; nth = use only the N-th segment (1-based)
    # Example: 63-ZB-丸子头女-张明-...  with sep='-', parts=3, take='nth' → 丸子头女
    custom_take: str = "nth"
    only_files: bool = True


@dataclass
class MovePlan:
    src: Path
    dest: Path
    group: str


@dataclass
class OrganizeReport:
    folders_created: int = 0
    files_moved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    plans: list[MovePlan] = field(default_factory=list)


@dataclass
class PlanResult:
    plans: list[MovePlan] = field(default_factory=list)
    scanned: int = 0
    already_in_place: int = 0
    skipped_small_group: int = 0
    group_sizes: dict[str, int] = field(default_factory=dict)
    message: str = ""


def safe_folder_name(name: str) -> str:
    name = (name or "").strip() or "未命名"
    name = _BAD_FOLDER_CHARS.sub("_", name)
    name = name.rstrip(" .")
    return name[:120] or "未命名"


def group_key_for(path: Path, opt: OrganizeOptions) -> str:
    stem = path.stem
    mode = (opt.mode or "custom").lower()

    if mode == "prefix":
        m = _TRAILING_SEQ.match(stem)
        return safe_folder_name(m.group(1) if m else stem)

    if mode == "date":
        for pat in _DATE_PATTERNS:
            m = pat.search(stem)
            if m:
                return safe_folder_name(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        return "__未识别日期__"

    if mode == "ext":
        ext = path.suffix.lower() or ".无扩展名"
        if opt.friendly_types:
            return safe_folder_name(EXT_TYPE_MAP.get(ext, "其它"))
        return safe_folder_name(ext.lstrip(".") or "无扩展名")

    if mode == "custom":
        sep = opt.custom_sep if opt.custom_sep is not None else "_"
        n = max(1, int(opt.custom_parts or 1))
        take = (opt.custom_take or "nth").lower()
        if not sep:
            return safe_folder_name(stem[:n] if n < len(stem) else stem)
        parts = [p for p in stem.split(sep)]
        if not parts:
            return safe_folder_name(stem)
        if take in ("nth", "index", "one"):
            # 1-based index: 第 3 段 → parts[2]
            if n <= len(parts):
                key = parts[n - 1]
            else:
                key = parts[-1]
        else:
            # join first N segments (old behavior)
            key = sep.join(parts[:n])
        key = (key or "").strip()
        return safe_folder_name(key or stem)

    return safe_folder_name(stem)


def iter_files(root: Path, *, recursive: bool) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        return
    if recursive:
        for p in root.rglob("*"):
            if p.is_file():
                # skip files already inside a first-level organized subfolder? No — user chooses root.
                # But avoid moving files that are not directly under root when we only want top-level:
                yield p
    else:
        for p in root.iterdir():
            if p.is_file():
                yield p


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    parent = dest.parent
    i = 2
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def plan_moves(root: Path | str, opt: OrganizeOptions | None = None) -> list[MovePlan]:
    """Dry-run: compute destination for each file under root."""
    return plan_moves_detailed(root, opt).plans


def plan_moves_detailed(root: Path | str, opt: OrganizeOptions | None = None) -> PlanResult:
    """Dry-run with diagnostics (why files were skipped)."""
    opt = opt or OrganizeOptions()
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    result = PlanResult()
    # Collect movable candidates by group (files not already in their target folder)
    groups: dict[str, list[Path]] = {}
    already = 0
    scanned = 0
    for f in iter_files(root, recursive=opt.recursive):
        scanned += 1
        key = group_key_for(f, opt)
        dest_dir = root / key
        try:
            in_place = f.parent.resolve() == dest_dir.resolve()
        except Exception:
            in_place = f.parent == dest_dir
        if in_place:
            already += 1
            continue
        groups.setdefault(key, []).append(f)

    result.scanned = scanned
    result.already_in_place = already
    min_n = max(1, int(opt.min_group_size or 1))
    plans: list[MovePlan] = []
    skipped_small = 0

    def _count_already_in_dest(key: str) -> int:
        """Files already sitting in root/<key>/ count toward group size."""
        dest_dir = root / key
        if not dest_dir.is_dir():
            return 0
        n = 0
        try:
            for p in dest_dir.iterdir():
                if p.is_file() and group_key_for(p, opt) == key:
                    n += 1
        except Exception:
            pass
        return n

    for key, files in sorted(groups.items(), key=lambda kv: kv[0].lower()):
        in_dest = _count_already_in_dest(key)
        total_in_group = len(files) + in_dest
        result.group_sizes[key] = total_in_group
        # If target folder already exists, always allow moving leftovers into it
        # (even a single leftover), because the group is already established.
        dest_exists = (root / key).is_dir()
        if total_in_group < min_n and not dest_exists:
            skipped_small += len(files)
            continue
        dest_dir = root / key
        for f in files:
            plans.append(MovePlan(src=f, dest=dest_dir / f.name, group=key))

    result.plans = plans
    result.skipped_small_group = skipped_small
    if not plans:
        bits = [f"扫描到 {scanned} 个文件"]
        if already:
            bits.append(f"{already} 个已在对应文件夹中")
        if skipped_small:
            bits.append(f"{skipped_small} 个因「同组不足 {min_n} 个」未移动")
        if scanned == 0:
            bits.append("目录下没有文件（若文件在子文件夹里，请勾选「包含子文件夹中的文件」）")
        elif already == scanned:
            bits.append("全部文件都已在目标文件夹内，无需再移动")
        elif skipped_small and not plans:
            bits.append("可取消勾选「仅当同组 ≥ 2 个」后再预览")
        result.message = "；".join(bits) + "。"
    else:
        result.message = (
            f"扫描 {scanned} 个文件 → 将移动 {len(plans)} 个到 {len(summarize_plans(plans))} 个文件夹"
            + (f"（另有 {already} 个已在位）" if already else "")
        )
    return result


def summarize_plans(plans: list[MovePlan]) -> dict[str, list[MovePlan]]:
    out: dict[str, list[MovePlan]] = {}
    for p in plans:
        out.setdefault(p.group, []).append(p)
    return out


def execute_moves(
    plans: list[MovePlan],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> OrganizeReport:
    report = OrganizeReport(plans=list(plans))
    total = len(plans)
    created: set[str] = set()
    for i, plan in enumerate(plans, start=1):
        try:
            dest_dir = plan.dest.parent
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                if str(dest_dir) not in created:
                    created.add(str(dest_dir))
                    report.folders_created += 1
            dest = unique_dest(plan.dest)
            shutil.move(str(plan.src), str(dest))
            report.files_moved += 1
            if on_progress:
                on_progress(i, total, f"{plan.src.name} → {dest_dir.name}/")
        except Exception as e:
            report.errors.append(f"{plan.src.name}: {e}")
            report.skipped += 1
            if on_progress:
                on_progress(i, total, f"失败 {plan.src.name}")
    return report
