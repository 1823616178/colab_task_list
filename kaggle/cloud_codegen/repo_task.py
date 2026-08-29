"""仓库改造评测任务: 模型带工具改代码, 跑仓库自己的验收命令, 断言取真实退出码。

这是一个真评测: 断言用的是验收命令的实际退出码, 模型改不好就是红的。
改动会以 unified diff + 改后文件的形式写进输出目录, 随 run 产物打包下载。

⚠ 必须单文件自包含: kaggle b t push 只推这一个 .py 并转成 notebook,
  云端不存在兄弟模块可以 import, 所以配置只能是下面的常量块。
"""

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import kaggle_benchmarks as kbench
from kaggle_benchmarks.tools.base import ToolInvocationLimitExhausted
from kaggle_benchmarks.tools.native import native_tool_agent

# ==== 配置区: 换任务只改这里 ================================================

# 仓库目录名。云端在 /kaggle/input/<dataset>/ 下按这个名字找; 本地用 CODEGEN_REPO_PATH。
REPO_NAME = "example_repo"

SPEC = """\
这个仓库的测试有失败项。修改 textstats 包的实现, 让全部测试通过。

要求:
- 只改实现, 不要改 tests/ 下的任何文件
- 保持公开函数的签名不变
- 保留类型注解和 docstring
"""

# 验收命令: 退出码为 0 即通过。这就是评测的事实依据。
VERIFY_COMMAND = [sys.executable, "-m", "pytest", "-q"]

# 模型不允许修改的路径前缀(相对仓库根), 防止它改测试来蒙混过关。
PROTECTED_PREFIXES = ("tests/", "pytest.ini")

MAX_TOOL_ROUNDS = 16
MAX_FILE_BYTES = 60_000
VERIFY_TIMEOUT_SECONDS = 300
VERIFY_OUTPUT_TAIL = 4_000

# ==== 运行期状态 ============================================================

OUTPUT_DIRECTORY = Path("/kaggle/working") if Path("/kaggle/working").is_dir() else Path.cwd()
WORKSPACE = OUTPUT_DIRECTORY / "workspace"
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", ".ruff_cache", ".mypy_cache"}
IGNORED_NAMES = shutil.ignore_patterns(*IGNORED_PARTS, "*.pyc")


def is_tracked(path: Path) -> bool:
    """判断是不是仓库内容; 缓存目录和字节码不算, 免得污染 diff 和文件列表。"""
    return (
        path.is_file()
        and not set(path.parts) & IGNORED_PARTS
        and path.suffix != ".pyc"
    )


class ProtectedPathError(Exception):
    """模型试图改动受保护的文件。"""


def locate_repository() -> Path:
    """定位仓库源目录: 优先环境变量, 其次 /kaggle/input, 最后当前目录旁边。"""
    override = os.environ.get("CODEGEN_REPO_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"CODEGEN_REPO_PATH 指向的目录不存在: {path}")
        return path

    kaggle_input = Path("/kaggle/input")
    if kaggle_input.is_dir():
        for candidate in sorted(kaggle_input.rglob(REPO_NAME)):
            if candidate.is_dir():
                return candidate
        raise FileNotFoundError(
            f"在 /kaggle/input 下找不到 {REPO_NAME}/, "
            f"push 时是否忘了用 -d 挂载数据集?"
        )

    fallback = (Path.cwd() / REPO_NAME).resolve()
    if fallback.is_dir():
        return fallback
    raise FileNotFoundError(f"找不到仓库 {REPO_NAME}, 请设置 CODEGEN_REPO_PATH")


def prepare_workspace(repository: Path) -> dict[str, str]:
    """把仓库复制一份到工作目录, 返回改动前的文本快照(用于生成 diff)。"""
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    shutil.copytree(repository, WORKSPACE, ignore=IGNORED_NAMES)

    snapshot: dict[str, str] = {}
    for path in sorted(WORKSPACE.rglob("*")):
        if is_tracked(path):
            try:
                snapshot[str(path.relative_to(WORKSPACE))] = path.read_text()
            except UnicodeDecodeError:
                continue
    return snapshot


def resolve_inside_workspace(path: str, for_writing: bool) -> Path:
    """把模型给的相对路径解析到工作目录内, 拒绝逃逸和受保护路径。"""
    resolved = (WORKSPACE / path).resolve()
    if not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ProtectedPathError(f"路径越界: {path}")
    relative = str(resolved.relative_to(WORKSPACE.resolve()))
    if for_writing and relative.startswith(PROTECTED_PREFIXES):
        raise ProtectedPathError(f"{relative} 是受保护文件, 不允许修改")
    return resolved


def execute_verification() -> subprocess.CompletedProcess:
    """在工作目录执行验收命令。"""
    return subprocess.run(
        VERIFY_COMMAND,
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=VERIFY_TIMEOUT_SECONDS,
    )


# ==== 暴露给模型的工具 ======================================================


def list_files() -> str:
    """列出仓库里所有文件的相对路径。"""
    paths = [
        str(path.relative_to(WORKSPACE))
        for path in sorted(WORKSPACE.rglob("*"))
        if is_tracked(path)
    ]
    return "\n".join(paths)


def read_file(path: str) -> str:
    """读取仓库内一个文件的全部内容。path 是相对仓库根的路径。"""
    try:
        target = resolve_inside_workspace(path, for_writing=False)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    if not target.is_file():
        return f"ERROR: 文件不存在: {path}"
    return target.read_text()[:MAX_FILE_BYTES]


def write_file(path: str, content: str) -> str:
    """把内容整体写入仓库内的一个文件, 覆盖原有内容。tests/ 下的文件不可写。"""
    try:
        target = resolve_inside_workspace(path, for_writing=True)
    except ProtectedPathError as error:
        return f"ERROR: {error}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"OK: 已写入 {path} ({len(content)} 字符)"


def run_verification() -> str:
    """运行仓库的验收命令(测试), 返回退出码和输出, 用来检查改动是否正确。"""
    result = execute_verification()
    output = (result.stdout + result.stderr)[-VERIFY_OUTPUT_TAIL:]
    return f"exit_code={result.returncode}\n\n{output}"


TOOLS = [list_files, read_file, write_file, run_verification]


# ==== 产物 ==================================================================


def build_diff(snapshot: dict[str, str]) -> str:
    """对比快照与当前工作目录, 生成 unified diff。"""
    chunks: list[str] = []
    current: dict[str, str] = {}
    for path in sorted(WORKSPACE.rglob("*")):
        if is_tracked(path):
            try:
                current[str(path.relative_to(WORKSPACE))] = path.read_text()
            except UnicodeDecodeError:
                continue

    for relative in sorted(set(snapshot) | set(current)):
        before = snapshot.get(relative, "")
        after = current.get(relative, "")
        if before == after:
            continue
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    return "".join(chunks)


def write_artifacts(diff_text: str, result: subprocess.CompletedProcess) -> None:
    """把 diff 和验收报告写进输出目录, 随 run 产物打包。"""
    (OUTPUT_DIRECTORY / "changes.diff").write_text(diff_text or "(无改动)\n")
    (OUTPUT_DIRECTORY / "verification.txt").write_text(
        f"command={' '.join(VERIFY_COMMAND)}\n"
        f"exit_code={result.returncode}\n\n{result.stdout}\n{result.stderr}"
    )


# ==== 任务 ==================================================================


@kbench.task(
    name="repo-codegen",
    description="让模型带工具修改一个真实仓库, 用仓库自己的测试判定是否完成",
)
def repo_codegen(llm) -> bool:
    """模型用工具改仓库, 断言取验收命令的真实退出码。"""
    repository = locate_repository()
    print(f"仓库来源: {repository}", flush=True)
    snapshot = prepare_workspace(repository)
    print(f"工作副本: {WORKSPACE} ({len(snapshot)} 个文本文件)", flush=True)

    before = execute_verification()
    print(f"改动前验收退出码: {before.returncode}", flush=True)

    prompt = (
        f"{SPEC}\n"
        "你可以用提供的工具查看和修改仓库文件, 并随时运行验收命令检查结果。\n"
        "请一直改到验收命令退出码为 0 为止, 然后简短说明你改了什么。"
    )
    kbench.user.send(prompt)
    try:
        native_tool_agent(llm, TOOLS, max_tool_rounds=MAX_TOOL_ROUNDS)
    except ToolInvocationLimitExhausted:
        print(f"⚠ 工具调用轮数用尽({MAX_TOOL_ROUNDS} 轮), 直接进入最终验收。", flush=True)

    result = execute_verification()
    diff_text = build_diff(snapshot)
    write_artifacts(diff_text, result)

    print("=== 改动 ===", flush=True)
    print(diff_text or "(无改动)", flush=True)
    print("=== 最终验收 ===", flush=True)
    print(f"exit_code={result.returncode}", flush=True)
    print(result.stdout[-2000:], flush=True)

    kbench.assertions.assert_equal(
        0, result.returncode,
        expectation=f"改完后 `{' '.join(VERIFY_COMMAND)}` 要通过",
    )
    return result.returncode == 0


if os.environ.get("CODEGEN_IMPORT_ONLY") != "1":
    repo_codegen.run(kbench.llm)  # type: ignore[attr-defined]
