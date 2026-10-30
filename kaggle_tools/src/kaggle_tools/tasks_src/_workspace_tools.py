"""三个 Benchmark 任务共用的工作区与工具实现。

⚠ 这不是可单独 import 的模块: kcloud build 会把它内联进每个任务文件,
  生成可单文件 push 的版本(kaggle b t push 只上传一个 .py)。
  函数在运行时按名字查找 WORKSPACE / PROTECTED_PREFIXES / execute_verification 等全局量,
  所以任务文件可以在内联之后重新赋值来定制行为。
"""

import difflib
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
# 验收命令超时(秒), 可用 CODEGEN_VERIFY_TIMEOUT 调整, 非法值退回 300。
try:
    VERIFY_TIMEOUT_SECONDS = int(os.environ.get("CODEGEN_VERIFY_TIMEOUT", "300"))
except ValueError:
    VERIFY_TIMEOUT_SECONDS = 300
if VERIFY_TIMEOUT_SECONDS <= 0:
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


def unified_diff(relative: str, before: str | None, after: str | None) -> str:
    """生成 git apply 能直接应用的 unified diff。

    新增文件用 /dev/null 作旧路径, 删除文件用 /dev/null 作新路径;
    末行没有换行符时补 "\\ No newline at end of file" 标记。
    """
    before_lines = (before or "").splitlines(keepends=True)
    after_lines = (after or "").splitlines(keepends=True)
    lines = list(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="/dev/null" if before is None else f"a/{relative}",
        tofile="/dev/null" if after is None else f"b/{relative}",
    ))
    output = []
    for line in lines:
        if line.endswith("\n"):
            output.append(line)
        else:
            output.append(line + "\n\\ No newline at end of file\n")
    return "".join(output)


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


def truncation_notice(text: str, shown: int) -> str:
    """截断提示: 告诉模型全文多大、怎么分段读、不能整份覆盖。"""
    return (
        f"\n\n[已截断: 文件共 {len(text)} 字符 / {text.count(chr(10)) + 1} 行, 仅显示前 {shown} 字符。"
        " 用 read_file(path, start_line, end_line) 分段读取, 用 edit_file 局部修改;"
        " 此文件禁止用 write_file 整份覆盖。]"
    )


def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """读取工作区内一个文件。path 是相对工作区根的路径。

    start_line / end_line 是从 1 开始的闭区间行号, 都为 0 表示读全文;
    大文件会被截断并附带提示, 此时请分段读取。
    """
    try:
        target = resolve_inside_workspace(path, for_writing=False)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    if not target.is_file():
        return f"ERROR: 文件不存在: {path}"
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: {path} 不是 UTF-8 文本文件"

    if start_line or end_line:
        lines = text.splitlines(keepends=True)
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        if start > end:
            return f"ERROR: 行号范围无效: {start_line}-{end_line}(文件共 {len(lines)} 行)"
        chunk = "".join(lines[start - 1:end])
        header = f"[第 {start}-{end} 行, 共 {len(lines)} 行]\n"
        if len(chunk) > MAX_FILE_BYTES:
            return header + chunk[:MAX_FILE_BYTES] + truncation_notice(chunk, MAX_FILE_BYTES)
        return header + chunk

    if len(text) > MAX_FILE_BYTES:
        return text[:MAX_FILE_BYTES] + truncation_notice(text, MAX_FILE_BYTES)
    return text


def write_file(path: str, content: str) -> str:
    """把内容整体写入工作区内的一个文件(新建或覆盖)。受保护路径不可写。

    已存在且超过读取上限的大文件不允许整份覆盖, 请用 edit_file。
    """
    try:
        target = resolve_inside_workspace(path, for_writing=True)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            existing = ""
        if len(existing) > MAX_FILE_BYTES:
            return (
                f"ERROR: {path} 有 {len(existing)} 字符, 超过读取上限 {MAX_FILE_BYTES}, "
                "整份覆盖会丢失你没读到的部分。请用 edit_file 局部修改。"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"OK: 已写入 {path} ({len(content)} 字符)"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """把文件中唯一出现的一段 old_text 替换成 new_text, 用于局部修改(大文件必须用它)。

    old_text 必须与文件内容逐字一致且只出现一次; 想追加内容可以把相邻的已有文本一并作为 old_text。
    """
    try:
        target = resolve_inside_workspace(path, for_writing=True)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    if not target.is_file():
        return f"ERROR: 文件不存在: {path}(新文件请用 write_file)"
    if not old_text:
        return "ERROR: old_text 不能为空"
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"ERROR: {path} 不是 UTF-8 文本文件"
    count = text.count(old_text)
    if count == 0:
        return "ERROR: 没找到 old_text, 请先用 read_file 确认原文(包括缩进和空白)"
    if count > 1:
        return f"ERROR: old_text 出现了 {count} 次, 请多带几行上下文使其唯一"
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"OK: 已修改 {path}"


def run_verification() -> str:
    """运行验收命令(测试), 返回退出码和输出, 用来检查改动是否正确。"""
    result = execute_verification()  # noqa: F821 - 由任务文件定义
    output = (result.stdout + result.stderr)[-VERIFY_OUTPUT_TAIL:]
    return f"exit_code={result.returncode}\n\n{output}"
