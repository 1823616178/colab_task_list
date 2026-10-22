"""评测任务的驱动: 本地试跑 -> 打包数据集 -> push -> run -> 下载产物。

先用 local 把任务调通再 push: Kaggle 服务端不支持删除 task
(kaggle_api_extended.py:10329 的 delete 是空壳, SDK 里也没有 delete 端点),
每次 push 都是永久记录。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import __version__
from ..common.config import OPENAI_PATH_SUFFIX, Settings, load_settings
from ..common.errors import ConfigError, KaggleToolsError
from ..common.kaggle_cli import current_username, run_kaggle
from ..common.logging import get_logger, setup_logging
from .build import SOURCE_DIRECTORY, build_tasks

logger = get_logger(__name__)

TASK_DIRECTORY = SOURCE_DIRECTORY
CODEGEN_JOB_FILE = "codegen-job.json"
STAGING_MARKER = "dataset-metadata.json"

# 三个任务: repo 是仓库改造，spec/codegen 是固定场景示例。
TASKS = {
    "repo": ("repo-codegen", TASK_DIRECTORY / "repo_task.py"),
    "spec": ("spec-codegen", TASK_DIRECTORY / "spec_task.py"),
    "codegen": ("cloud-codegen", TASK_DIRECTORY / "codegen_task.py"),
}

# 运行期配置; main() 里按环境变量重新加载。
SETTINGS: Settings = load_settings(environ={}, env_file=Path(os.devnull))
RUNS_DIRECTORY = SETTINGS.runs_dir


def task_of(args: argparse.Namespace) -> tuple[str, Path]:
    """取出当前选中任务的 slug 与源文件。"""
    return TASKS[args.task]


def built_task(args: argparse.Namespace) -> tuple[str, Path]:
    """构建单文件任务并返回 (slug, 生成文件)。push 和 local 都跑生成物, 保证一致。"""
    slug, source = task_of(args)
    build_tasks(RUNS_DIRECTORY / "tasks")
    return slug, RUNS_DIRECTORY / "tasks" / source.name


def prompt_of(args: argparse.Namespace) -> str | None:
    """读取命令行提示词；未指定时返回 None，让任务使用内置 SPEC。"""
    prompt_file = getattr(args, "prompt_file", None)
    prompt = getattr(args, "prompt", None)
    if prompt_file:
        path = Path(prompt_file).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"提示词文件不存在: {path}")
        try:
            prompt = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"读取提示词文件失败: {path}: {error}") from error
    if prompt is None:
        return None
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    return prompt


def add_prompt_arguments(parser: argparse.ArgumentParser) -> None:
    """给子命令添加互斥的短提示词和提示词文件参数。"""
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-p", "--prompt", default=None, help="repo 任务的短提示词")
    group.add_argument("--prompt-file", default=None, help="UTF-8 多行提示词文件")


def add_job_arguments(parser: argparse.ArgumentParser) -> None:
    """repo 任务的运行期覆盖项, 免得换个仓库就得改 tasks/repo_task.py。"""
    parser.add_argument(
        "--verify", default=None,
        help="验收命令(一行 shell 风格), 默认 python -m pytest -q",
    )
    parser.add_argument(
        "--protected", default=None,
        help="不许模型改的路径前缀, 逗号分隔; 传空串表示都能改(仓库从零生成时用)",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=None,
        help="工具调用轮数上限, 默认 16; 从零生成整个仓库要往上调",
    )


def job_overrides(args: argparse.Namespace) -> dict:
    """收集命令行上给出的 job 覆盖项, 没给的键不出现。"""
    overrides: dict[str, object] = {}
    if args.verify is not None:
        overrides["verify_command"] = args.verify
    if args.protected is not None:
        overrides["protected_prefixes"] = args.protected
    if args.max_rounds is not None:
        overrides["max_tool_rounds"] = args.max_rounds
    return overrides


def build_proxy_environment() -> dict[str, str]:
    """复用 kllm 的凭据管理, 组出本地跑任务所需的环境变量。"""
    from ..kllm.credentials import TokenManager

    credentials = TokenManager(SETTINGS).get()
    base_url = credentials.base_url
    if base_url.endswith(OPENAI_PATH_SUFFIX):
        base_url = base_url[: -len(OPENAI_PATH_SUFFIX)]
    return {
        "MODEL_PROXY_URL": base_url,
        "MODEL_PROXY_API_KEY": credentials.token,
        "LLM_DEFAULT": os.environ.get("LLM_DEFAULT", SETTINGS.cloud_default_model),
        "LLM_DEFAULT_EVAL": os.environ.get("LLM_DEFAULT_EVAL", SETTINGS.cloud_default_model),
    }


def kaggle_command(arguments: list[str]) -> int:
    """执行一条 kaggle 命令并透传输出, 带超时。"""
    print(f"$ kaggle {' '.join(arguments)}", flush=True)
    completed = run_kaggle(arguments, timeout=SETTINGS.kaggle_cli_timeout_seconds)
    if completed.returncode != 0:
        logger.error("kaggle %s 退出码 %s", arguments[0], completed.returncode)
    return completed.returncode


def confirm(question: str, assume_yes: bool) -> bool:
    """交互确认; 非交互环境(无 tty)且没给 -y 时视为拒绝, 避免卡死。"""
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print(f"{question} -> 非交互环境, 需要 -y 才会继续。", file=sys.stderr)
        return False
    return input(f"{question} [y/N] ").strip().lower() == "y"


def command_local(args: argparse.Namespace) -> int:
    """在本地跑任务(算力是自己的, 只花模型额度), 用于 push 前调通。"""
    try:
        prompt = prompt_of(args)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if prompt is not None and args.task != "repo":
        print("--prompt/--prompt-file 目前只支持 repo 任务", file=sys.stderr)
        return 1

    _, task_file = built_task(args)
    environment = dict(os.environ)
    environment.update(build_proxy_environment())
    if args.model:
        environment["LLM_DEFAULT"] = args.model
    if args.repo:
        environment["CODEGEN_REPO_PATH"] = str(Path(args.repo).resolve())
    if args.spec:
        environment["SPEC_PATH"] = str(Path(args.spec).resolve())
    if prompt is not None:
        environment["CODEGEN_PROMPT"] = prompt
    if args.verify is not None:
        environment["CODEGEN_VERIFY"] = args.verify
    if args.protected is not None:
        environment["CODEGEN_PROTECTED"] = args.protected
    if args.max_rounds is not None:
        environment["CODEGEN_MAX_ROUNDS"] = str(args.max_rounds)
    if args.resume:
        environment["CODEGEN_RESUME"] = "1"

    work_directory = Path(args.output).resolve()
    work_directory.mkdir(parents=True, exist_ok=True)
    print(f"任务: {task_file.name}")
    print(f"模型: {environment['LLM_DEFAULT']}")
    print(f"产物: {work_directory}\n")
    return subprocess.run(
        [sys.executable, str(task_file)], env=environment, cwd=work_directory
    ).returncode


def command_build(args: argparse.Namespace) -> int:
    """只生成单文件任务, 便于检查或手动 push。"""
    output = Path(args.output).resolve()
    for path in build_tasks(output):
        print(path)
    return 0


def command_dataset(args: argparse.Namespace) -> int:
    """把仓库打包成 Kaggle 私有数据集, 供云端任务通过 -d 挂载。"""
    try:
        prompt = prompt_of(args)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    if prompt is not None and args.task != "repo":
        print("--prompt/--prompt-file 目前只支持 repo 任务", file=sys.stderr)
        return 1

    repository = Path(args.repo).resolve()
    if not repository.is_dir():
        print(f"仓库目录不存在: {repository}", file=sys.stderr)
        if str(repository).startswith("/kaggle/input"):
            print(
                "  /kaggle/input/... 是云端运行时的挂载路径, 本机没有这个目录。\n"
                "  dataset 命令是把【本地】目录上传成数据集; 数据集建好后直接用:\n"
                "    kcloud push -d <owner>/<slug>",
                file=sys.stderr,
            )
        return 1

    owner = args.owner or current_username()
    if not owner:
        print("拿不到用户名, 用 --owner 指定。", file=sys.stderr)
        return 1

    slug = args.slug or repository.name.replace("_", "-")
    staging = Path(args.staging).resolve()
    if (
        staging == repository
        or staging in repository.parents
        or repository in staging.parents
    ):
        print("--staging 不能与仓库目录重叠", file=sys.stderr)
        return 1
    if staging.exists() and any(staging.iterdir()):
        if not (staging / STAGING_MARKER).is_file():
            print(
                f"--staging {staging} 已存在且不是 kcloud 生成的目录(缺少 {STAGING_MARKER}), "
                "为防误删已中止; 请换一个空目录。",
                file=sys.stderr,
            )
            return 1
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(
        repository,
        staging / repository.name,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "*.pyc"),
    )
    (staging / "dataset-metadata.json").write_text(
        json.dumps({"title": slug, "id": f"{owner}/{slug}", "licenses": [{"name": "CC0-1.0"}]},
                   indent=2)
    )
    # 云端靠 repo_name 在 /kaggle/input 下找仓库, 这里直接写死成实际上传的目录名,
    # 免得云端 REPO_NAME 与数据集内容对不上。
    job = {"repo_name": repository.name, **job_overrides(args)}
    if prompt is not None:
        job["prompt"] = prompt
    (staging / CODEGEN_JOB_FILE).write_text(
        json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.dry_run:
        print(f"[dry-run] 已在 {staging} 准备好数据集 {owner}/{slug}, 未上传。")
        return 0
    print(f"⚠ 这会把 {repository} 的内容上传到 Kaggle(私有数据集 {owner}/{slug})。")
    if not confirm("确认上传?", args.yes):
        print("已取消。")
        return 1

    action = ["datasets", "version", "-p", str(staging), "-m", args.message] if args.update \
        else ["datasets", "create", "-p", str(staging)]
    # Kaggle CLI 默认跳过子目录。用 zip 模式上传，repo_task 会在云端解包。
    action += ["--dir-mode", "zip"]
    exit_code = kaggle_command(action)
    if exit_code == 0:
        print(f"\n挂载用: kcloud push -d {owner}/{slug}")
    return exit_code


def command_push(args: argparse.Namespace) -> int:
    """把任务推到 Kaggle。注意: 不可删除, 不可撤销。"""
    slug, task_file = built_task(args)
    print(f"⚠ push 会在 Kaggle 上创建永久记录(源码 + backing notebook), 服务端不支持删除。")
    print(f"  任务: {slug}   文件: {task_file.name}")
    if args.dataset:
        print(f"  挂载数据集: {', '.join(args.dataset)}")
    if not confirm("确认推送?", args.yes):
        print("已取消。")
        return 1

    arguments = ["b", "t", "push", slug, "-f", str(task_file),
                 "--wait", str(SETTINGS.cloud_push_wait_seconds)]
    for dataset in args.dataset:
        arguments += ["-d", dataset]
    return kaggle_command(arguments)


def command_run(args: argparse.Namespace) -> int:
    """在云端跑一次任务。"""
    slug, _ = task_of(args)
    arguments = ["b", "t", "run", slug, "-m", args.model]
    if args.wait:
        arguments += ["--wait", str(args.wait)]
    return kaggle_command(arguments)


def command_status(args: argparse.Namespace) -> int:
    """查看任务与各模型的运行状态。"""
    return kaggle_command(["b", "t", "status", task_of(args)[0]])


def command_log(args: argparse.Namespace) -> int:
    """拉取云端运行日志。"""
    return kaggle_command(["b", "t", "log", task_of(args)[0], "-m", args.model])


def command_download(args: argparse.Namespace) -> int:
    """下载 run 产物并列出文件；Kaggle CLI 会自行解压输出包。"""
    output = Path(args.output).resolve()
    exit_code = kaggle_command(
        ["b", "t", "download", task_of(args)[0], "-m", args.model, "-o", str(output)]
    )
    if exit_code != 0:
        return exit_code
    print("\n下载到的文件:")
    for path in sorted(output.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(output)}  ({path.stat().st_size} B)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="kcloud", description="Kaggle 评测任务驱动")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出 DEBUG 日志")
    parser.add_argument("-t", "--task", choices=sorted(TASKS), default="repo",
                        help="选择任务(默认 repo: 仓库改造)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    local_parser = subparsers.add_parser("local", help="本地试跑(push 前必做)")
    local_parser.add_argument("-m", "--model", default=None)
    local_parser.add_argument("-r", "--repo", default=None, help="仓库路径(repo 任务用)")
    local_parser.add_argument("-s", "--spec", default=None, help="契约文档路径(spec 任务用)")
    local_parser.add_argument("-o", "--output", default=str(RUNS_DIRECTORY / "local"))
    local_parser.add_argument(
        "--resume", action="store_true",
        help="接着 -o 目录里上一轮的 workspace 改, 而不是从原始仓库重新复制",
    )
    add_prompt_arguments(local_parser)
    add_job_arguments(local_parser)
    local_parser.set_defaults(handler=command_local)

    dataset_parser = subparsers.add_parser("dataset", help="把仓库上传为私有数据集")
    dataset_parser.add_argument("-r", "--repo", required=True)
    dataset_parser.add_argument("--slug", default=None)
    dataset_parser.add_argument("--owner", default=None)
    dataset_parser.add_argument("--staging", default=str(RUNS_DIRECTORY / "dataset-staging"))
    dataset_parser.add_argument("--update", action="store_true", help="更新已存在的数据集")
    dataset_parser.add_argument("-m", "--message", default="update")
    dataset_parser.add_argument("-y", "--yes", action="store_true")
    dataset_parser.add_argument("--dry-run", action="store_true", help="只准备 staging, 不上传")
    add_prompt_arguments(dataset_parser)
    add_job_arguments(dataset_parser)
    dataset_parser.set_defaults(handler=command_dataset)

    push_parser = subparsers.add_parser("push", help="推送任务(永久, 不可删除)")
    push_parser.add_argument("-d", "--dataset", action="append", default=[],
                             help="挂载数据集 owner/slug, 可重复")
    push_parser.add_argument("-y", "--yes", action="store_true")
    push_parser.set_defaults(handler=command_push)

    run_parser = subparsers.add_parser("run", help="在云端运行")
    run_parser.add_argument("-m", "--model", default=SETTINGS.cloud_default_model)
    run_parser.add_argument("--wait", type=int, default=SETTINGS.cloud_run_wait_seconds)
    run_parser.set_defaults(handler=command_run)

    status_parser = subparsers.add_parser("status", help="查看状态")
    status_parser.set_defaults(handler=command_status)

    log_parser = subparsers.add_parser("log", help="查看云端日志")
    log_parser.add_argument("-m", "--model", default=SETTINGS.cloud_default_model)
    log_parser.set_defaults(handler=command_log)

    download_parser = subparsers.add_parser("download", help="下载并解压产物")
    download_parser.add_argument("-m", "--model", default=SETTINGS.cloud_default_model)
    download_parser.add_argument("-o", "--output", default=str(RUNS_DIRECTORY / "cloud"))
    download_parser.set_defaults(handler=command_download)

    build_parser_ = subparsers.add_parser("build", help="生成可单文件 push 的任务脚本")
    build_parser_.add_argument("-o", "--output", default=str(RUNS_DIRECTORY / "tasks"))
    build_parser_.set_defaults(handler=command_build)

    return parser


def configure(settings: Settings) -> None:
    """装载运行期配置(影响默认值, 所以要在构造解析器之前调用)。"""
    global SETTINGS, RUNS_DIRECTORY
    SETTINGS = settings
    RUNS_DIRECTORY = settings.runs_dir


def main(argv: list[str] | None = None) -> int:
    """命令行主入口: 可预期的错误打印一行提示并返回 1, 不打调用栈。"""
    try:
        configure(load_settings())
    except ConfigError as error:
        print(f"配置错误: {error}", file=sys.stderr)
        return 1
    args = build_parser().parse_args(argv)
    setup_logging("DEBUG" if args.verbose else SETTINGS.log_level, SETTINGS.log_file)
    try:
        return args.handler(args)
    except KaggleToolsError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
