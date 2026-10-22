"""仓库改造评测任务: 模型带工具改代码, 跑仓库自己的验收命令, 断言取真实退出码。

这是一个真评测: 断言用的是验收命令的实际退出码, 模型改不好就是红的。
改动会以 unified diff + 改后文件的形式写进输出目录, 随 run 产物打包下载。

⚠ 必须单文件自包含: kaggle b t push 只推这一个 .py 并转成 notebook,
  云端不存在兄弟模块可以 import。动态提示词通过 Dataset 中的 codegen-job.json 传入。
"""

import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import kaggle_benchmarks as kbench
from _workspace_tools import *  # kcloud build 时内联
from kaggle_benchmarks.tools.base import ToolInvocationLimitExhausted
from kaggle_benchmarks.tools.native import native_tool_agent

# ==== 默认配置 ==============================================================

# 下面五项都能在运行时覆盖: 本地走环境变量, 云端走数据集里的 codegen-job.json。
# 换一个仓库不该需要改这个文件再重新 push —— 云端任务推上去就删不掉了。

# 仓库目录名。云端在 /kaggle/input/<dataset>/ 下按这个名字找; 本地用 CODEGEN_REPO_PATH。
DEFAULT_REPO_NAME = "example_repo"

DEFAULT_SPEC = """\
这个仓库的测试有失败项。修改 textstats 包的实现, 让全部测试通过。

要求:
- 只改实现, 不要改 tests/ 下的任何文件
- 保持公开函数的签名不变
- 保留类型注解和 docstring
"""

# 验收命令: 退出码为 0 即通过。这就是评测的事实依据。
DEFAULT_VERIFY_COMMAND = [sys.executable, "-m", "pytest", "-q"]

# 模型不允许修改的路径前缀(相对仓库根), 防止它改测试来蒙混过关。
# 仓库要从零生成时置空, 否则模型连测试都写不出来。
DEFAULT_PROTECTED_PREFIXES = ("tests/", "pytest.ini")

DEFAULT_MAX_TOOL_ROUNDS = 16

# 运行期真正生效的值, load_job() 按需重新绑定。
REPO_NAME = DEFAULT_REPO_NAME
SPEC = DEFAULT_SPEC
VERIFY_COMMAND = list(DEFAULT_VERIFY_COMMAND)
PROTECTED_PREFIXES = DEFAULT_PROTECTED_PREFIXES
MAX_TOOL_ROUNDS = DEFAULT_MAX_TOOL_ROUNDS

# job 键名 -> 本地环境变量名。环境变量优先于数据集里的 job 文件。
JOB_ENVIRONMENT_KEYS = {
    "prompt": "CODEGEN_PROMPT",
    "repo_name": "CODEGEN_REPO_NAME",
    "verify_command": "CODEGEN_VERIFY",
    "protected_prefixes": "CODEGEN_PROTECTED",
    "max_tool_rounds": "CODEGEN_MAX_ROUNDS",
}

CODEGEN_JOB_FILE = "codegen-job.json"


def locate_repository() -> Path:
    """定位仓库源目录: 优先环境变量, 其次 /kaggle/input, 最后当前目录旁边。"""
    override = os.environ.get("CODEGEN_REPO_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"CODEGEN_REPO_PATH 指向的目录不存在: {path}")
        return path

    kaggle_input = KAGGLE_INPUT
    if kaggle_input.is_dir():
        # Kaggle CLI 的 dataset --dir-mode zip 会把仓库目录上传成同名 ZIP。
        # 解到 /tmp，避免输入副本混进 /kaggle/working 的下载产物。
        for archive in sorted(kaggle_input.rglob(f"{REPO_NAME}.zip")):
            extracted = Path(tempfile.gettempdir()) / "cloud_codegen_input" / REPO_NAME
            if extracted.exists():
                shutil.rmtree(extracted)
            extracted.mkdir(parents=True)
            with zipfile.ZipFile(archive) as bundle:
                destination = extracted.resolve()
                for member in bundle.infolist():
                    target = (destination / member.filename).resolve()
                    if not target.is_relative_to(destination):
                        raise ValueError(f"仓库压缩包包含越界路径: {member.filename}")
                bundle.extractall(destination)
            return extracted

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


def read_job_file(kaggle_input: Path) -> dict:
    """读云端数据集里的 codegen-job.json; 没挂载或没这个文件时返回空配置。"""
    if not kaggle_input.is_dir():
        return {}

    job_files = sorted(kaggle_input.rglob(CODEGEN_JOB_FILE))
    if not job_files:
        return {}
    if len(job_files) > 1:
        raise RuntimeError(
            f"找到多个 {CODEGEN_JOB_FILE}: "
            + ", ".join(str(path) for path in job_files)
        )
    try:
        payload = json.loads(job_files[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"读取云端任务配置失败: {job_files[0]}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{job_files[0]} 的顶层必须是 JSON 对象")
    return payload


def read_job_environment() -> dict:
    """从环境变量收集覆盖项, 只收真正设置过的键(空串也算设置)。"""
    job = {}
    for key, name in JOB_ENVIRONMENT_KEYS.items():
        value = os.environ.get(name)
        if value is not None:
            job[key] = value
    return job


def coerce_command(value) -> list[str]:
    """验收命令可以写成参数列表, 也可以写成一行 shell 风格的字符串。"""
    if isinstance(value, str):
        command = shlex.split(value)
    elif isinstance(value, (list, tuple)):
        command = [str(item) for item in value]
    else:
        raise ValueError(
            f"verify_command 得是字符串或字符串列表, 收到 {type(value).__name__}"
        )
    if not command:
        raise ValueError("verify_command 不能为空")
    return command


def coerce_prefixes(value) -> tuple[str, ...]:
    """受保护前缀: 逗号分隔的字符串或列表; 全空表示不保护任何路径。"""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        raise ValueError(
            f"protected_prefixes 得是字符串或字符串列表, 收到 {type(value).__name__}"
        )
    return tuple(stripped for item in items if (stripped := item.strip()))


def coerce_rounds(value) -> int:
    """工具调用轮数上限, 必须是正整数。"""
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"max_tool_rounds 得是整数, 收到 {value!r}") from None
    if rounds < 1:
        raise ValueError(f"max_tool_rounds 至少是 1, 收到 {rounds}")
    return rounds


def load_job(kaggle_input: Path | None = None) -> str:
    """合并 job 配置并覆盖运行期常量, 返回最终提示词。

    环境变量优先于数据集里的 codegen-job.json: 本地想临时换个验收命令,
    不必重新上传数据集。
    """
    global REPO_NAME, VERIFY_COMMAND, PROTECTED_PREFIXES, MAX_TOOL_ROUNDS

    job = read_job_file(KAGGLE_INPUT if kaggle_input is None else kaggle_input)
    job.update(read_job_environment())

    repo_name = str(job.get("repo_name", "")).strip()
    if repo_name:
        REPO_NAME = repo_name
    if "verify_command" in job:
        VERIFY_COMMAND = coerce_command(job["verify_command"])
    if "protected_prefixes" in job:
        PROTECTED_PREFIXES = coerce_prefixes(job["protected_prefixes"])
    if "max_tool_rounds" in job:
        MAX_TOOL_ROUNDS = coerce_rounds(job["max_tool_rounds"])

    prompt = job.get("prompt")
    if prompt is None:
        return SPEC
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 得是非空字符串")
    return prompt.strip()


def prepare_workspace(repository: Path, resume: bool = False) -> dict[str, str]:
    """准备工作副本, 返回原始仓库的文本快照(用于生成 diff)。

    resume=True 且已有工作副本时保留它, 让模型接着上一轮的成果继续改。
    快照始终取自原始仓库, 所以 diff 永远是相对原始仓库的累计改动。
    """
    existing = WORKSPACE.is_dir() and any(
        path for path in WORKSPACE.rglob("*") if is_tracked(path)
    )
    if resume and existing:
        print(f"接着上一轮的工作副本改: {WORKSPACE}", flush=True)
    else:
        if existing:
            # 上一轮的成果就在这里, 删之前必须让人看见 —— 没加 --resume 时它会没。
            print(f"⚠ 覆盖已有工作副本 {WORKSPACE}(要接着改就加 --resume)", flush=True)
        if WORKSPACE.exists():
            shutil.rmtree(WORKSPACE)
        shutil.copytree(repository, WORKSPACE, ignore=IGNORED_NAMES)

    return read_tree(repository)


def execute_verification() -> subprocess.CompletedProcess:
    """在工作目录执行验收命令。"""
    return run_command(VERIFY_COMMAND, cwd=WORKSPACE)


TOOLS = [list_files, read_file, write_file, run_verification]


# ==== 产物 ==================================================================


def build_diff(snapshot: dict[str, str]) -> str:
    """对比快照与当前工作目录, 生成 unified diff。"""
    chunks: list[str] = []
    current = read_tree(WORKSPACE)

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
    (OUTPUT_DIRECTORY / "changes.diff").write_text(diff_text or "(无改动)\n", encoding="utf-8")
    (OUTPUT_DIRECTORY / "verification.txt").write_text(
        f"command={' '.join(VERIFY_COMMAND)}\n"
        f"exit_code={result.returncode}\n\n{result.stdout}\n{result.stderr}",
        encoding="utf-8",
    )


# ==== 任务 ==================================================================


@kbench.task(
    name="repo-codegen",
    description="让模型带工具修改一个真实仓库, 用仓库自己的测试判定是否完成",
)
def repo_codegen(llm) -> bool:
    """模型用工具改仓库, 断言取验收命令的真实退出码。"""
    # 先加载 job: REPO_NAME 和 VERIFY_COMMAND 后面就要用。
    task_spec = load_job()
    print(f"仓库名: {REPO_NAME}", flush=True)
    print(f"验收命令: {' '.join(VERIFY_COMMAND)}", flush=True)
    print(f"受保护路径: {', '.join(PROTECTED_PREFIXES) or '(无)'}", flush=True)
    print(f"工具轮数上限: {MAX_TOOL_ROUNDS}", flush=True)
    print(f"提示词: {len(task_spec)} 字符", flush=True)

    repository = locate_repository()
    print(f"仓库来源: {repository}", flush=True)
    snapshot = prepare_workspace(repository, resume=os.environ.get("CODEGEN_RESUME") == "1")
    print(f"工作副本: {WORKSPACE} ({len(snapshot)} 个文本文件)", flush=True)

    before = execute_verification()
    print(f"改动前验收退出码: {before.returncode}", flush=True)
    prompt = (
        f"{task_spec}\n"
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


if not import_only():
    repo_codegen.run(kbench.llm)  # type: ignore[attr-defined]
