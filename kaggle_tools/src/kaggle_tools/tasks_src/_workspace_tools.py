"""三个 Benchmark 任务共用的工作区与工具实现。

⚠ 这不是可单独 import 的模块: kcloud build 会把它内联进每个任务文件,
  生成可单文件 push 的版本(kaggle b t push 只上传一个 .py)。
  函数在运行时按名字查找 WORKSPACE / PROTECTED_PREFIXES / execute_verification 等全局量,
  所以任务文件可以在内联之后重新赋值来定制行为。
"""

import os
import shutil
import subprocess
from pathlib import Path

# 云端 notebook 的产物目录; 本地跑时退回到当前目录。
KAGGLE_WORKING = Path("/kaggle/working")
KAGGLE_INPUT = Path("/kaggle/input")
OUTPUT_DIRECTORY = KAGGLE_WORKING if KAGGLE_WORKING.is_dir() else Path.cwd()
WORKSPACE = OUTPUT_DIRECTORY / "workspace"

MAX_FILE_BYTES = 60_000
VERIFY_TIMEOUT_SECONDS = 300
VERIFY_OUTPUT_TAIL = 4_000

# 模型不允许修改的路径前缀(相对工作区根, POSIX 分隔符)。
PROTECTED_PREFIXES: tuple[str, ...] = ()

IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".ruff_cache", ".mypy_cache"}
IGNORED_NAMES = shutil.ignore_patterns(*IGNORED_PARTS, "*.pyc")


class ProtectedPathError(Exception):
    """模型试图访问工作区之外或受保护的路径。"""


def import_only() -> bool:
    """测试和构建校验时只 import 不运行任务。"""
    return "1" in (os.environ.get("CODEGEN_IMPORT_ONLY"), os.environ.get("CLOUD_CODEGEN_IMPORT_ONLY"))


def is_tracked(path: Path) -> bool:
    """判断是不是工作区内容; 缓存目录和字节码不算, 免得污染 diff 和文件列表。"""
    return path.is_file() and not set(path.parts) & IGNORED_PARTS and path.suffix != ".pyc"


def relative_posix(path: Path, root: Path) -> str:
    """相对路径统一用 / 分隔, Windows 上受保护前缀匹配和 diff 才一致。"""
    return path.relative_to(root).as_posix()


def read_tree(root: Path) -> dict[str, str]:
    """把一棵目录里的文本文件读成 {相对路径: 内容}, 跳过缓存和二进制。"""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if is_tracked(path):
            try:
                tree[relative_posix(path, root)] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
    return tree


def resolve_inside_workspace(path: str, for_writing: bool = False) -> Path:
    """把模型给的相对路径解析到工作区内, 拒绝逃逸和(写入时)受保护路径。"""
    root = WORKSPACE.resolve()
    resolved = (WORKSPACE / path).resolve()
    if not resolved.is_relative_to(root):
        raise ProtectedPathError(f"路径越界: {path}")
    relative = relative_posix(resolved, root)
    if for_writing and PROTECTED_PREFIXES and relative.startswith(PROTECTED_PREFIXES):
        raise ProtectedPathError(f"{relative} 是受保护文件, 不允许修改")
    return resolved


def run_command(command: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """跑验收命令; 超时不抛异常, 而是当成失败结果返回, 让模型能看到。"""
    try:
        return subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=VERIFY_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(
            command, 124, stdout=str(error.stdout or ""),
            stderr=f"验收命令超过 {VERIFY_TIMEOUT_SECONDS} 秒未结束, 已终止。",
        )
    except OSError as error:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=f"无法执行验收命令: {error}")


# ==== 暴露给模型的工具 ======================================================


def list_files() -> str:
    """列出工作区里所有文件的相对路径。"""
    paths = [relative_posix(path, WORKSPACE) for path in sorted(WORKSPACE.rglob("*")) if is_tracked(path)]
    return "\n".join(paths) or "(工作区为空)"


def read_file(path: str) -> str:
    """读取工作区内一个文件的全部内容。path 是相对工作区根的路径。"""
    try:
        target = resolve_inside_workspace(path, for_writing=False)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    if not target.is_file():
        return f"ERROR: 文件不存在: {path}"
    try:
        return target.read_text(encoding="utf-8")[:MAX_FILE_BYTES]
    except UnicodeDecodeError:
        return f"ERROR: {path} 不是 UTF-8 文本文件"


def write_file(path: str, content: str) -> str:
    """把内容整体写入工作区内的一个文件, 覆盖原有内容。受保护路径不可写。"""
    try:
        target = resolve_inside_workspace(path, for_writing=True)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"OK: 已写入 {path} ({len(content)} 字符)"


def run_verification() -> str:
    """运行验收命令(测试), 返回退出码和输出, 用来检查改动是否正确。"""
    result = execute_verification()  # noqa: F821 - 由任务文件定义
    output = (result.stdout + result.stderr)[-VERIFY_OUTPUT_TAIL:]
    return f"exit_code={result.returncode}\n\n{output}"
