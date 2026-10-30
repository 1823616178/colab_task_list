"""把 tasks_src/ 下的任务源码内联共享模块, 生成可单文件 push 的任务脚本。

任务源码里写一行 `from _workspace_tools import *`(行尾可带注释),
构建时把它替换成 _workspace_tools.py 的正文。生成物不再 import 任何兄弟模块。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SOURCE_DIRECTORY = Path(__file__).resolve().parent.parent / "tasks_src"
SHARED_MODULE = "_workspace_tools"
TASK_FILES = ("repo_task.py", "spec_task.py", "codegen_task.py")

INLINE_PATTERN = re.compile(rf"^from {SHARED_MODULE} import \*.*$", re.MULTILINE)
GENERATED_HEADER = (
    "# ⚠ 由 kcloud build 生成, 请勿手改; 源码在 src/kaggle_tools/tasks_src/{name}\n"
    "# 源码哈希: {digest} (任务源码 + 共享模块的 SHA-256 前 16 位, "
    "手动 push 旧生成物时可据此核对是否过期)\n"
)


class BuildError(RuntimeError):
    """任务源码不符合内联约定。"""


def shared_body(source_directory: Path = SOURCE_DIRECTORY) -> str:
    """共享模块正文(去掉模块 docstring)。"""
    text = (source_directory / f"{SHARED_MODULE}.py").read_text(encoding="utf-8")
    text = re.sub(r'\A""".*?"""\n', "", text, count=1, flags=re.DOTALL)
    return (
        f"# ==== 内联自 {SHARED_MODULE}.py " + "=" * 40 + "\n"
        + text.strip("\n")
        + f"\n# ==== {SHARED_MODULE}.py 结束 " + "=" * 42 + "\n"
    )


def render_task(name: str, source_directory: Path = SOURCE_DIRECTORY) -> str:
    """生成单个任务的单文件版本。"""
    source = (source_directory / name).read_text(encoding="utf-8")
    matches = INLINE_PATTERN.findall(source)
    if len(matches) != 1:
        raise BuildError(f"{name} 必须恰好有一行 `from {SHARED_MODULE} import *`, 找到 {len(matches)}")
    body = shared_body(source_directory)
    rendered = INLINE_PATTERN.sub(lambda _match: body, source, count=1)
    digest = hashlib.sha256(
        (source + body).encode("utf-8")
    ).hexdigest()[:16]
    return GENERATED_HEADER.format(name=name, digest=digest) + rendered


def build_tasks(output_directory: Path, source_directory: Path = SOURCE_DIRECTORY) -> list[Path]:
    """生成全部任务到 output_directory, 返回生成的文件路径。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in TASK_FILES:
        target = output_directory / name
        target.write_text(render_task(name, source_directory), encoding="utf-8", newline="\n")
        written.append(target)
    return written
