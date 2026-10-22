"""运行产物目录: 每次运行一个带时间戳的新目录, 并维护 latest 指针, 不再互相覆盖。

布局:
    runs/<kind>/<YYYYmmdd-HHMMSS>[-label]/   kind = local | cloud
    runs/<kind>/latest                       指向该类最近一次(符号链接; 不支持时为 latest.txt)
    runs/latest                              指向任意类最近一次
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

LATEST = "latest"
LATEST_FILE = "latest.txt"


def slugify(text: str) -> str:
    """把模型名等转换成适合做目录名的短串。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60]


def new_run_dir(runs_root: Path, kind: str, label: str = "", now: datetime | None = None) -> Path:
    """创建并返回一个新的运行目录; 同一秒内重复时追加序号。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{slugify(label)}" if label and slugify(label) else stamp
    parent = runs_root / kind
    parent.mkdir(parents=True, exist_ok=True)
    candidate, index = parent / base, 1
    while candidate.exists():
        index += 1
        candidate = parent / f"{base}-{index}"
    candidate.mkdir()
    return candidate


def _point(link_dir: Path, target: Path) -> None:
    """在 link_dir 下让 latest 指向 target: 优先相对符号链接, 失败(如 Windows 无权限)写 latest.txt。"""
    link = link_dir / LATEST
    text_file = link_dir / LATEST_FILE
    if link.is_symlink() or link.is_file():
        link.unlink()
    try:
        link.symlink_to(os.path.relpath(target, link_dir), target_is_directory=True)
        if text_file.exists():
            text_file.unlink()
    except (OSError, NotImplementedError):
        text_file.write_text(str(target.resolve()) + "\n", encoding="utf-8")


def mark_latest(runs_root: Path, kind: str, run_dir: Path) -> None:
    """更新 runs/<kind>/latest 与 runs/latest。"""
    _point(runs_root / kind, run_dir)
    _point(runs_root, run_dir)


def latest_run(runs_root: Path, kind: str | None = None) -> Path | None:
    """读取 latest 指针; 没有时返回 None。"""
    link_dir = runs_root / kind if kind else runs_root
    link = link_dir / LATEST
    if link.is_symlink() or link.is_dir():
        resolved = link.resolve()
        return resolved if resolved.is_dir() else None
    text_file = link_dir / LATEST_FILE
    if text_file.is_file():
        target = Path(text_file.read_text(encoding="utf-8").strip())
        return target if target.is_dir() else None
    return None
